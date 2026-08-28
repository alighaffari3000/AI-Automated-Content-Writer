"""The daily article pipeline, as an ADK 2.x workflow graph.

    load context -> research -> build registry -> write
                                                    |
                                 +------------------+
                                 v
                         three reviewers (parallel, blind to each other)
                                 |
                                 v
                          deterministic gate
                           |             |
                     approve/out      revise
                           |             |
                           v             v
                        finalize       judge -> back to write (max N rounds)

Two rules shape this graph. Decisions that matter are plain functions, so the
same reviews always yield the same verdict. And the writer may only use facts
the registry sanctioned, so an invented specification cannot reach the site.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.models import Gemini
from google.adk.tools import google_search
from google.adk.workflow import DEFAULT_ROUTE, JoinNode, Workflow
from google.genai import types

from . import prompts, seo, structured_data
from .config import settings
from .images import ImageGenerator, ImageRequest, find_markers, replace_markers
from .notify import build_notifier
from .normalize import claim_key
from .sources import (
    SourceIndex,
    audit_fact,
    authority_from,
    check_reachable,
    fetch_pages,
    harvest,
)
from .rules import evaluate_reviews
from .schemas import (
    ArticleDraft,
    ResearchBundle,
    ReviewResult,
    RevisionDirective,
    TopicProposal,
)
from .site_client import SiteClient
from .store import Store

logger = logging.getLogger(__name__)

REVIEWER_NODES = ("technical_fact_reviewer", "product_reviewer", "seo_editorial_reviewer")


def agent_models() -> dict[str, str]:
    """Which model each agent runs on.

    Runner events carry the agent's name but not the model's, so accounting
    has to be told. Keep this in step with the builders below or a run's cost
    is quietly attributed to a model with no configured rate — which shows up
    as a suspiciously free article rather than as an error.
    """
    worker, author = settings.models.worker, settings.models.author
    return {
        "topic_planner": worker,
        "researcher": worker,
        "fact_builder": author,
        "writer": author,
        "judge": author,
        **{name: worker for name in REVIEWER_NODES},
    }

_store: Store | None = None


def get_store() -> Store:
    """Opened on first use so that importing this module touches no disk."""
    global _store
    if _store is None:
        _store = Store(settings.db_path)
    return _store


def get_site() -> SiteClient:
    return SiteClient(settings.site, dry_run=settings.dry_run)


def _model(name: str) -> Gemini:
    return Gemini(model=name, retry_options=types.HttpRetryOptions(attempts=3))


def _as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _from_state(ctx: Context, key: str) -> list[Any]:
    """Read back something this pipeline put into state as JSON.

    State also carries prose for the prompts — "(none available)" where a fetch
    came back empty — so anything unparseable is simply no data.
    """
    raw = ctx.state.get(key)
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


# --------------------------------------------------------------------- nodes


def load_run_context(ctx: Context, node_input: Any) -> Event:
    """Choose whose turn it is, and gather everything the planner needs.

    Only the category is decided here, and deterministically — the subject of
    the article itself is invented a step later. Picking by fewest-articles
    -written keeps coverage even across subject areas instead of drifting
    toward whichever one is easiest to write about.

    The catalogue and the archive are fetched here rather than handed to an
    agent as tools: a reviewer that can be told what a product costs cannot
    hallucinate what it costs, and the search-grounded researcher keeps its one
    built-in tool.
    """
    store = get_store()
    site = get_site()

    category = store.next_category()
    if category is None:
        return Event(
            output={"status": "no_category"},
            route="no_category",
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="No subject areas are defined; nothing to write about."
                    )
                ],
            ),
        )

    products = site.products()
    articles = site.published_articles()
    taxonomy = site.taxonomy()
    recent = store.recent_titles()
    covered = store.topics_in_category(category["id"])
    # slugs_from may hand back full URLs where the site sent permalinks;
    # unlinked() compares against "/slug" in bodies, so reduce each to the
    # bare slug the way SiteIndex does, or every linked page reads as orphaned.
    orphans = store.unlinked(
        [seo.slug_of(s) or s.lower() for s in seo.slugs_from(articles)]
    )

    requested_subject = os.environ.get("REQUESTED_SUBJECT", "")
    logger.info(
        "Run started: category=%r (%s article(s) so far) products=%s published=%s%s",
        category["name"],
        category["times_used"],
        len(products),
        len(articles),
        f" requested_subject={requested_subject!r}" if requested_subject else "",
    )

    return Event(
        output={"category": category["name"]},
        route="ok",
        state={
            "category_id": category["id"],
            "category_name": category["name"],
            "category_description": category["description"],
            "category_audience": category["audience"],
            "topics_in_category": (
                _as_json(covered) if covered else "(nothing covered here yet)"
            ),
            "site_products": _as_json(products) if products else "(none available)",
            "site_articles": _as_json(
                [a.get("title", "") for a in articles] + recent
            ),
            # The addresses that exist, kept apart from the titles the prompts
            # read: the gate checks links and slugs against these, and a list
            # meant for a model to read is not a list code can check against.
            "site_slugs": (
                seo.slugs_from(articles)
                + seo.slugs_from(products)
                + seo.slugs_from(taxonomy.get("categories") or [])
            ),
            "site_taxonomy": (
                _as_json(taxonomy)
                if taxonomy["categories"]
                else "(the site reported no categories; leave category empty)"
            ),
            # The article this cluster hangs off, and the pages nothing points
            # at yet. Both are link targets the writer is asked to prefer:
            # structure that is asked for while the article is being written
            # costs nothing, and structure retrofitted afterwards never happens.
            "pillar_slug": category["pillar_slug"] or "",
            "link_targets": (
                _as_json(orphans) if orphans else "(nothing is short of links)"
            ),
            "claimed_keywords": _as_json(sorted(store.claimed_keywords())[:120]),
            # `run --topic "..."` hands the planner a subject instead of
            # letting it invent one. Env rather than a parameter because this
            # bootstrap runs inside the ADK graph, which nothing else threads
            # arguments through.
            "requested_subject": requested_subject,
            "rejected_subjects": "",
            "topic_attempt": 0,
            "round_number": 0,
            "revision_directive": "",
        },
    )


# One retry. A planner that proposes a subject already covered gets told so and
# asked again; a planner that does it twice is describing a category that is
# genuinely written out, and the honest answer is to stop rather than to
# publish something competing with what is already there.
MAX_TOPIC_ATTEMPTS = 2


def cannibalises(keywords: list[str], claimed: dict[str, str]) -> str:
    """Whether this subject would compete with one already published.

    Only when *every* query it targets is already spoken for. Sharing one broad
    keyword is normal and healthy — two articles about batteries both mention
    batteries. Sharing all of them means the same searcher is being answered
    twice, and the two pieces split the ranking between them.
    """
    keys = [claim_key(k) for k in keywords if claim_key(k)]
    if not keys:
        return ""
    owners = [claimed.get(key, "") for key in keys]
    return owners[0] if all(owners) else ""


def open_article(ctx: Context, node_input: Any) -> Event:
    """Record the subject the planner invented and start an article for it."""
    proposal = ctx.state.get("topic_proposal") or {}
    title = str(proposal.get("title", "")).strip()
    category_id = int(ctx.state.get("category_id", 0))

    if not title:
        return Event(
            output={"status": "no_topic"},
            route="no_topic",
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="The planner proposed no subject.")],
            ),
        )

    store = get_store()
    proposed = [str(k) for k in (proposal.get("keywords") or [])]
    attempt = int(ctx.state.get("topic_attempt", 0)) + 1

    # Nothing is recorded until this passes: a subject that is sent back must
    # leave no trace, or the planner would be avoiding it for ever after.
    competing = cannibalises(proposed, store.claimed_keywords())
    if competing:
        rejected = str(ctx.state.get("rejected_subjects") or "")
        note = f"{title} — every query it targets already belongs to {competing!r}"
        logger.info("Subject rejected as a duplicate (attempt %s): %s", attempt, note)
        if attempt >= MAX_TOPIC_ATTEMPTS:
            return Event(
                output={"status": "no_topic"},
                route="no_topic",
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_text(
                            text="Every subject the planner proposed competes with "
                            "one already published."
                        )
                    ],
                ),
            )
        return Event(
            output={"status": "duplicate", "topic": title},
            route="retry",
            state={
                "topic_attempt": attempt,
                "rejected_subjects": f"{rejected}\n- {note}".strip(),
            },
        )

    keywords = ", ".join(proposed)
    topic_id = store.record_topic(
        category_id, title, str(proposal.get("angle", "")), keywords
    )
    store.mark_category_used(category_id)
    article_id = store.start_article(topic_id)

    known = recall_facts(title, proposed)

    logger.info(
        "Today's subject: %r (article_id=%s, %s fact(s) recalled from the registry)",
        title,
        article_id,
        len(known),
    )

    return Event(
        output={"topic": title, "article_id": article_id},
        route="ok",
        state={
            "article_id": article_id,
            "topic_id": topic_id,
            "topic_title": title,
            "topic_notes": str(proposal.get("angle", "")),
            "topic_keywords": keywords,
            "known_facts": known,
            "known_facts_prompt": prompts.format_known_facts(known),
        },
    )


def recall_facts(title: str, keywords: list[str]) -> list[dict[str, Any]]:
    """What the registry already knows that bears on today's subject.

    Each one arrives with a citable id of its own, so a fact reused from the
    registry passes the same audit as a fact found this morning — it cites a
    source the pipeline can see, which is the rule that makes the registry
    trustworthy rather than merely convenient.
    """
    if not settings.registry.enabled:
        return []

    terms = [w for w in re.split(r"[\s,،/]+", f"{title} {' '.join(keywords)}") if w]
    rows = get_store().live_facts(terms, limit=settings.registry.max_reused)
    return [
        {
            "reg_id": f"reg-{number}",
            "claim_key": row["claim_key"],
            "claim": row["claim"],
            "kind": row["kind"],
            "confidence": row["confidence"],
            "evidence": row["evidence"],
            "source": row["source"],
            "source_url": row["source_url"],
            "tier": row["tier"],
            "tier_label": row["tier_label"],
            "verified": bool(row["verified"]),
            "verified_at": (row["verified_at"] or "")[:10],
            "expires_at": (row["expires_at"] or "")[:10],
        }
        for number, row in enumerate(rows, start=1)
    ]


def collect_sources(callback_context: CallbackContext) -> None:
    """Harvest the URLs the search actually used, once the researcher is done.

    Grounding puts its sources in the response metadata, not in the model's
    text. Read from the session's events rather than trusting the prose, or
    every citation in the registry is whatever the model chose to type.

    The registry's own sources join the same list here, with ids of their own.
    A fact reused from the registry then cites something the audit can look up,
    exactly like a fact found this morning — one rule for citations, not two.
    """
    session = callback_context._invocation_context.session  # noqa: SLF001
    index = harvest(
        list(session.events),
        SourceIndex(authority=authority_from(settings.sources)),
    )
    if index:
        check_reachable(index)

    for known in callback_context.state.get("known_facts") or []:
        index.add_known(
            short_id=str(known.get("reg_id")),
            url=str(known.get("source_url") or ""),
            title=str(known.get("source") or ""),
            tier=int(known.get("tier") or 1),
            tier_label=str(known.get("tier_label") or "general web"),
            verified_at=str(known.get("verified_at") or ""),
        )

    callback_context.state["source_index"] = index.to_list()
    callback_context.state["sources_for_prompt"] = index.as_prompt()
    logger.info("Grounding produced %s distinct source(s).", len(index.sources))


def persist_registry(ctx: Context, node_input: Any) -> Event:
    """Audit every fact against its real sources, then store the registry.

    This is the step that closes the loop the reviewers cannot: they check the
    article against the registry, and nothing checked the registry against the
    world. Here each fact meets the sources the search actually returned —
    a cited id that was never in that list invalidates the fact outright, and
    a confident technical claim resting only on the general web is downgraded.
    All of it is code, so the same research always earns the same verdict.
    """
    raw = ctx.state.get("research_bundle") or {}
    bundle = ResearchBundle(**raw) if raw else ResearchBundle(angle="", outline=[])

    index = SourceIndex.from_list(
        ctx.state.get("source_index") or [],
        authority=authority_from(settings.sources),
    )

    # Read only the pages some fact actually leans on. A search returns twenty
    # sources and an article rests on six, so fetching the rest would be paying
    # for pages nobody cited.
    fetch_pages(
        index,
        [sid for fact in bundle.facts for sid in fact.source_ids],
        settings.sources,
    )

    downgraded = 0
    for fact in bundle.facts:
        audit = audit_fact(
            fact.source_ids,
            index,
            fact.confidence,
            evidence=fact.evidence,
            config=settings.sources,
        )
        if audit.confidence != fact.confidence or audit.allowed != fact.allowed:
            downgraded += 1
        fact.confidence = audit.confidence
        fact.allowed = fact.allowed and audit.allowed
        fact.audit_note = audit.note
        fact.verified = audit.verified
        # The strongest source it cites, not the first: this URL is what a
        # reader following the claim ends up at.
        cited = [s for s in (index.get(sid) for sid in fact.source_ids) if s]
        fact.source_url = max(cited, key=lambda s: s.tier).url if cited else ""

    allowed = bundle.allowed_facts
    article_id = int(ctx.state.get("article_id", 0))
    if article_id:
        get_store().save_facts(
            article_id, [f.model_dump() for f in bundle.facts]
        )

    remember(bundle, index, ctx)

    logger.info(
        "Fact registry: %s registered, %s usable, %s verified at the source, "
        "%s adjusted by the audit.",
        len(bundle.facts),
        len(allowed),
        sum(1 for f in allowed if f.verified),
        downgraded,
    )
    if not allowed:
        return Event(
            output={"status": "no_facts"},
            route="no_facts",
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="Research produced no usable facts; stopping before writing."
                    )
                ],
            ),
        )

    return Event(
        output=bundle.model_dump(),
        route="ok",
        state={
            "target_keywords": ", ".join(bundle.target_keywords),
            # Put the index back with what reading the pages taught us —
            # publication dates, pages that turned out unreadable — so the
            # reviewers judge the sources as they finally stood.
            "source_index": index.to_list(),
            "sources_for_prompt": index.as_prompt(),
        },
    )


def site_index(ctx: Context) -> seo.SiteIndex:
    """The pages the site said it has, as the gate needs them."""
    return seo.SiteIndex.from_slugs(
        [str(s) for s in _from_state(ctx, "site_slugs")],
        extra_paths=settings.seo.known_paths,
        public_url=settings.site.public_url,
    )


def remember(bundle: ResearchBundle, index: SourceIndex, ctx: Context) -> None:
    """Hand what survived the audit to the registry, with a shelf life.

    Only what the audit accepted, and only in the two forms worth reusing: a
    passage found where it was cited, or a source whose authority was never in
    question. Everything else has to be looked up again tomorrow, which is the
    correct price for not having checked it today.
    """
    if not settings.registry.enabled:
        return

    store = get_store()
    article_id = int(ctx.state.get("article_id", 0))
    rows = []
    for fact in bundle.allowed_facts:
        cited = [s for s in (index.get(sid) for sid in fact.source_ids) if s]
        # A fact citing the registry at all was reused, not re-verified — even
        # when a fresh source rides along, because the audit trusts registry
        # sources unread, so `verified` proves nothing about the fresh one.
        # Storing it again would push the expiry date forward on every reuse,
        # which is how a shelf life quietly becomes no shelf life at all and a
        # stale price outlives the year it was true. Re-verification happens
        # the honest way: the shelf life runs out, the registry stops offering
        # the fact, and the researcher meets the claim as new evidence again.
        if any(s.from_registry for s in cited):
            continue
        best = max(cited, key=lambda s: s.tier) if cited else None
        rows.append(
            {
                **fact.model_dump(),
                "tier": best.tier if best else 1,
                "tier_label": best.tier_label if best else "",
                "source_url": best.url if best else fact.source_url,
            }
        )
    stored = store.remember_facts(article_id, rows, settings.registry.shelf_life)

    # Count a reuse where one actually happened: a fact that cited the
    # registry's own id. The count is keyed by the *stored* claim_key, carried
    # in known_facts — keying by today's wording would lose the count whenever
    # the model reworded the claim, and the reuse number is what this whole
    # feature is justified by.
    stored_keys = {
        str(k.get("reg_id")): str(k.get("claim_key") or "")
        for k in ctx.state.get("known_facts") or []
    }
    reused = {
        key
        for fact in bundle.allowed_facts
        for sid in fact.source_ids
        if (key := stored_keys.get(sid))
    }
    store.mark_facts_used(sorted(reused))
    if stored or reused:
        logger.info(
            "Registry: %s claim(s) remembered, %s reused from earlier runs.",
            stored,
            len(reused),
        )


def evaluate_gate(ctx: Context, node_input: dict[str, Any]) -> Event:
    """The gate: plain code, one verdict, always the same answer for the same input.

    `node_input` arrives from the join as {reviewer_node_name: review_dict}.

    Two kinds of finding meet here. The reviewers bring judgement, scored. The
    measurements bring counting — lengths, heading levels, alt text, a slug
    that is already taken, a link that resolves to nothing — and those are not
    scored at all, because the same draft must always earn the same verdict on
    them.
    """
    reviews: list[ReviewResult] = []
    for node_name in REVIEWER_NODES:
        payload = (node_input or {}).get(node_name)
        if not payload:
            logger.warning("Reviewer %s produced nothing.", node_name)
            continue
        try:
            reviews.append(ReviewResult(**payload))
        except Exception as exc:  # noqa: BLE001 - a malformed review is not a verdict
            logger.warning("Reviewer %s returned an unusable result: %s", node_name, exc)

    draft = ArticleDraft(**(ctx.state.get("draft") or {}))
    bundle = ResearchBundle(**(ctx.state.get("research_bundle") or {}))
    round_number = int(ctx.state.get("round_number", 0)) + 1

    measured = seo.defects(
        draft, site_index(ctx), bundle.target_keywords, settings.seo
    )

    decision = evaluate_reviews(
        draft=draft,
        bundle=bundle,
        reviews=reviews,
        round_number=round_number,
        config=settings.quality,
        measured=measured,
        safety=settings.safety,
    )

    article_id = int(ctx.state.get("article_id", 0))
    if article_id:
        store = get_store()
        for review in reviews:
            store.save_review(
                article_id,
                round_number,
                review.reviewer,
                review.score,
                [i.model_dump() for i in review.issues],
                review.summary,
            )

    logger.info(
        "Round %s verdict: %s (avg %s, %s measured defect(s)) - %s",
        round_number,
        decision.verdict,
        decision.average_score,
        len(measured),
        decision.reason,
    )

    # Approved and exhausted both end the run; which of the two it was travels
    # in the payload, so the graph needs only one terminal route.
    route = "revise" if decision.verdict == "REVISE" else "finalize"
    return Event(
        output=decision.model_dump(),
        route=route,
        state={
            "round_number": round_number,
            "gate_reason": decision.reason,
            "gate_decision": decision.model_dump(),
            "reviews_json": _as_json([r.model_dump() for r in reviews]),
            "measured_issues": (
                _as_json([i.model_dump() for i in measured])
                if measured
                else "(nothing measurable is wrong with the draft)"
            ),
        },
    )


def illustrate(draft: ArticleDraft) -> tuple[str, str, int, float | None]:
    """Make the article's pictures and put them where they belong.

    Runs once, after the gate has approved — never during a revision round,
    where every picture would be paid for and then thrown away.

    Returns the body with images in place, the lead image's URL, how many
    pictures were generated, and what the provider said they cost where it
    said at all. The last two exist so the run can be costed, since these
    calls do not pass through the runner. All of it degrades quietly: a failed
    picture leaves an article with one fewer, which is a far better outcome
    than no article.
    """
    if not settings.images.enabled:
        return replace_markers(draft.body, []), "", 0, None

    site = get_site()
    generator = ImageGenerator(settings.images)
    stem = draft.slug or "article"
    generated = 0
    # None until a provider reports one, so "nobody said" stays distinct from
    # "it was free" and the estimate is used for the pictures nobody priced.
    billed: float | None = None

    featured_url = ""
    if draft.featured_image_prompt:
        image = generator.generate(
            ImageRequest(
                draft.featured_image_prompt,
                draft.featured_image_alt,
                draft.featured_image_style,
            )
        )
        if image:
            generated += 1
            if image.cost_usd is not None:
                billed = (billed or 0.0) + image.cost_usd
            featured_url = (
                site.upload_image(
                    image.data, f"{stem}-lead{image.extension}", draft.featured_image_alt
                )
                or ""
            )

    requests = find_markers(draft.body)[: settings.images.max_in_body]
    urls: list[str | None] = []
    for index, request in enumerate(requests, start=1):
        image = generator.generate(request)
        if image is None:
            urls.append(None)
            continue
        generated += 1
        if image.cost_usd is not None:
            billed = (billed or 0.0) + image.cost_usd
        urls.append(
            site.upload_image(image.data, f"{stem}-{index}{image.extension}", request.alt)
        )

    made = sum(1 for u in urls if u)
    logger.info(
        "Illustration: lead=%s, %s of %s in-body image(s) placed.",
        "yes" if featured_url else "no",
        made,
        len(requests),
    )
    # Any marker past the cap is dropped rather than published as literal text.
    return replace_markers(draft.body, urls), featured_url, generated, billed


def finalize(ctx: Context, node_input: dict[str, Any]) -> Event:
    """Send the draft to the site, record the outcome, tell a human.

    Every article leaves as a draft awaiting approval — including one the gate
    approved. Autonomy is a later decision, and this is the line that holds it.
    """
    decision = node_input or {}
    verdict = str(decision.get("verdict", "ESCALATE"))
    draft_data = ctx.state.get("draft") or {}
    draft = ArticleDraft(**draft_data) if draft_data else None
    article_id = int(ctx.state.get("article_id", 0))
    round_number = int(ctx.state.get("round_number", 0))
    store = get_store()

    if draft is None:
        if article_id:
            store.fail_article(article_id, "The pipeline finished without a draft.")
        message = "Run finished without producing a draft."
        build_notifier(settings.notify).send(message)
        return Event(
            output={"status": "no_draft"},
            content=types.Content(
                role="model", parts=[types.Part.from_text(text=message)]
            ),
        )

    illustrated_body, featured_url, images_made, images_billed = illustrate(draft)

    # Built after illustration so the FAQ answers match the body that ships,
    # and from the catalogue rather than the prose, so a product specification
    # cannot reach a rich result by way of a sentence.
    bundle_data = ctx.state.get("research_bundle") or {}
    schema_blocks = structured_data.build(
        draft,
        body=illustrated_body,
        site=settings.site,
        content=settings.content,
        config=settings.seo,
        products=_from_state(ctx, "site_products"),
        keywords=list(bundle_data.get("target_keywords") or []),
        featured_image=featured_url,
    )
    if schema_blocks:
        logger.info(
            "Structured data: %s",
            ", ".join(block.get("@type", "?") for block in schema_blocks),
        )

    ok, remote = get_site().create_post(
        title=draft.title,
        slug=draft.slug,
        excerpt=draft.excerpt,
        body=illustrated_body,
        status="draft",
        featured_image=featured_url,
        category=draft.category,
        tags=draft.tags,
        seo_title=draft.seo_title,
        meta_description=draft.meta_description,
        related_products=draft.related_products,
        related_solutions=draft.related_solutions,
        structured_data=schema_blocks,
        meta={
            "generated_by": "ai-content-writer",
            "verdict": verdict,
            "rounds": round_number,
            "average_score": decision.get("average_score"),
            "used_fact_ids": draft.used_fact_ids,
            "featured_image_alt": draft.featured_image_alt,
            "requires_human": decision.get("requires_human") or [],
        },
    )

    stored = draft.model_dump()
    stored["body"] = illustrated_body
    stored["featured_image"] = featured_url

    store.finish_article(
        article_id,
        draft=stored,
        status="draft_sent" if ok else "send_failed",
        verdict=verdict,
        rounds=round_number,
        remote_id=remote if ok else "",
        error="" if ok else remote,
    )

    needs_person = decision.get("requires_human") or []
    headline = (
        "A draft is ready for review"
        if verdict == "APPROVE"
        else "A draft needs your judgement"
    )
    lines = [
        f"<b>{headline}</b>",
        f"Title: {draft.title}",
        f"Verdict: {verdict} after {round_number} round(s), "
        f"average score {decision.get('average_score')}",
        f"Reason: {decision.get('reason', '')}",
    ]
    if needs_person:
        # Said separately from the reason, because this is the part that asks
        # the reader of this message to do something rather than to be informed.
        lines.append("⚠️ Read this one properly: " + "; ".join(needs_person))
    if not ok:
        lines.append(f"⚠️ Could not reach the site: {remote}")
    message = "\n".join(lines)
    build_notifier(settings.notify).send(message)

    logger.info("Finalised article %s: verdict=%s sent=%s", article_id, verdict, ok)
    return Event(
        output={
            "status": "draft_sent" if ok else "send_failed",
            "verdict": verdict,
            "title": draft.title,
            "remote_id": remote if ok else "",
        },
        state={"images_generated": images_made, "images_billed_usd": images_billed},
        content=types.Content(role="model", parts=[types.Part.from_text(text=message)]),
    )


def abort_run(ctx: Context, node_input: dict[str, Any]) -> Event:
    """Nothing to write, or nothing worth writing from. Say so and stop."""
    status = str((node_input or {}).get("status", "aborted"))
    reasons = {
        "no_category": (
            "No subject areas are defined. Add one with "
            "`python -m app.cli categories add \"...\"`."
        ),
        "no_topic": "The planner proposed no subject for this category.",
        "no_facts": "Research found no source solid enough to write from.",
    }
    reason = reasons.get(status, "The run stopped early.")

    article_id = int(ctx.state.get("article_id", 0))
    category_id = int(ctx.state.get("category_id", 0))
    topic_id = int(ctx.state.get("topic_id", 0))
    if article_id:
        get_store().fail_article(article_id, reason)
    # Give the turn back, so a failed run does not cost this category its slot.
    if category_id and status != "no_category":
        get_store().release_category(category_id)
    # And un-log the subject: nothing was published, so the planner must stay
    # free to propose it again.
    if topic_id:
        get_store().discard_topic(topic_id)

    if status != "no_category":
        build_notifier(settings.notify).send(f"Today's run stopped: {reason}")
    logger.warning("Run aborted (%s): %s", status, reason)
    return Event(
        output={"status": status},
        content=types.Content(role="model", parts=[types.Part.from_text(text=reason)]),
    )


# -------------------------------------------------------------------- agents


def build_topic_planner() -> LlmAgent:
    """Invents today's subject inside the category whose turn it is."""
    return LlmAgent(
        name="topic_planner",
        model=_model(settings.models.worker),
        instruction=prompts.topic_planner_instruction(settings.content),
        output_schema=TopicProposal,
        output_key="topic_proposal",
    )


def build_researcher() -> LlmAgent:
    return LlmAgent(
        name="researcher",
        model=_model(settings.models.worker),
        instruction=prompts.researcher_instruction(settings.content),
        tools=[google_search],
        output_key="research_notes",
        after_agent_callback=collect_sources,
    )


def build_fact_builder() -> LlmAgent:
    """The author tier, despite being a one-shot transformation.

    This is not the summarising step it looks like. It decides which of the
    researcher's claims are checkable at all, which source actually supports
    each one, and which passage to quote — and everything downstream rests on
    that: the writer may only state what is registered here, and the gate
    verifies the article against it. A cheap model reads the same notes and
    registers nothing, which does not fail loudly. It ends the run with
    "no source solid enough to write from" and a research bill already paid.
    """
    return LlmAgent(
        name="fact_builder",
        model=_model(settings.models.author),
        instruction=prompts.fact_builder_instruction(settings.content),
        output_schema=ResearchBundle,
        output_key="research_bundle",
    )


def build_writer() -> LlmAgent:
    return LlmAgent(
        name="writer",
        model=_model(settings.models.author),
        instruction=prompts.writer_instruction(
            settings.content, settings.images, settings.seo
        ),
        output_schema=ArticleDraft,
        output_key="draft",
    )


def build_reviewers() -> tuple[LlmAgent, LlmAgent, LlmAgent]:
    """Three reviewers that never see each other's findings.

    Blindness is not automatic in a parallel branch — it holds because each
    instruction is given only the draft and the registry, and no reviewer's
    output key appears in another's prompt.
    """
    technical = LlmAgent(
        name="technical_fact_reviewer",
        model=_model(settings.models.worker),
        instruction=prompts.technical_reviewer_instruction(settings.content),
        output_schema=ReviewResult,
        output_key="review_technical",
    )
    product = LlmAgent(
        name="product_reviewer",
        model=_model(settings.models.worker),
        instruction=prompts.product_reviewer_instruction(settings.content),
        output_schema=ReviewResult,
        output_key="review_product",
    )
    editorial = LlmAgent(
        name="seo_editorial_reviewer",
        model=_model(settings.models.worker),
        instruction=prompts.editorial_reviewer_instruction(settings.content),
        output_schema=ReviewResult,
        output_key="review_editorial",
    )
    return technical, product, editorial


def build_judge() -> LlmAgent:
    """Runs only when the gate asks for a revision — never to approve anything."""
    return LlmAgent(
        name="judge",
        model=_model(settings.models.author),
        instruction=prompts.JUDGE_INSTRUCTION,
        output_schema=RevisionDirective,
        output_key="revision_directive",
    )


# --------------------------------------------------------------------- graph


def build_pipeline() -> Workflow:
    topic_planner = build_topic_planner()
    researcher = build_researcher()
    fact_builder = build_fact_builder()
    writer = build_writer()
    technical, product, editorial = build_reviewers()
    judge = build_judge()
    join_reviews = JoinNode(name="join_reviews")

    return Workflow(
        name="content_pipeline",
        description=(
            "Researches, writes, reviews and revises one article per run, then "
            "hands it to a human as a draft."
        ),
        edges=[
            ("START", load_run_context),
            (load_run_context, {"ok": topic_planner, "no_category": abort_run}),
            (topic_planner, open_article),
            (
                open_article,
                {"ok": researcher, "retry": topic_planner, "no_topic": abort_run},
            ),
            (researcher, fact_builder),
            (fact_builder, persist_registry),
            (persist_registry, {"ok": writer, "no_facts": abort_run}),
            (writer, (technical, product, editorial)),
            ((technical, product, editorial), join_reviews),
            (join_reviews, evaluate_gate),
            (evaluate_gate, {"finalize": finalize, "revise": judge}),
            (judge, writer),
        ],
    )


root_agent = build_pipeline()

app = App(
    root_agent=root_agent,
    name="app",
)
