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
from .sources import SourceIndex, audit_fact, check_reachable, harvest
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
        "fact_builder": worker,
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

    logger.info(
        "Run started: category=%r (%s article(s) so far) products=%s published=%s",
        category["name"],
        category["times_used"],
        len(products),
        len(articles),
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
            "round_number": 0,
            "revision_directive": "",
        },
    )


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
    keywords = ", ".join(proposal.get("keywords") or [])
    topic_id = store.record_topic(
        category_id, title, str(proposal.get("angle", "")), keywords
    )
    store.mark_category_used(category_id)
    article_id = store.start_article(topic_id)

    logger.info("Today's subject: %r (article_id=%s)", title, article_id)

    return Event(
        output={"topic": title, "article_id": article_id},
        route="ok",
        state={
            "article_id": article_id,
            "topic_id": topic_id,
            "topic_title": title,
            "topic_notes": str(proposal.get("angle", "")),
            "topic_keywords": keywords,
        },
    )


def collect_sources(callback_context: CallbackContext) -> None:
    """Harvest the URLs the search actually used, once the researcher is done.

    Grounding puts its sources in the response metadata, not in the model's
    text. Read from the session's events rather than trusting the prose, or
    every citation in the registry is whatever the model chose to type.
    """
    session = callback_context._invocation_context.session  # noqa: SLF001
    index = harvest(list(session.events))
    if index:
        check_reachable(index)
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

    index = SourceIndex()
    for entry in ctx.state.get("source_index") or []:
        source = index.add(
            url=str(entry.get("url", "")),
            title=str(entry.get("title", "")),
            domain=str(entry.get("domain", "")),
        )
        source.reachable = entry.get("reachable")
        source.note = str(entry.get("note", ""))

    downgraded = 0
    for fact in bundle.facts:
        confidence, allowed, note = audit_fact(fact.source_ids, index, fact.confidence)
        if confidence != fact.confidence or allowed != fact.allowed:
            downgraded += 1
        fact.confidence = confidence
        fact.allowed = fact.allowed and allowed
        fact.audit_note = note
        urls = [s.url for s in (index.get(sid) for sid in fact.source_ids) if s]
        fact.source_url = urls[0] if urls else ""

    allowed = bundle.allowed_facts
    article_id = int(ctx.state.get("article_id", 0))
    if article_id:
        get_store().save_facts(
            article_id, [f.model_dump() for f in bundle.facts]
        )

    logger.info(
        "Fact registry: %s registered, %s usable, %s adjusted by the source audit.",
        len(bundle.facts),
        len(allowed),
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
        state={"target_keywords": ", ".join(bundle.target_keywords)},
    )


def site_index(ctx: Context) -> seo.SiteIndex:
    """The pages the site said it has, as the gate needs them."""
    return seo.SiteIndex.from_slugs(
        [str(s) for s in _from_state(ctx, "site_slugs")],
        extra_paths=settings.seo.known_paths,
        public_url=settings.site.public_url,
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


def illustrate(draft: ArticleDraft) -> tuple[str, str, int]:
    """Make the article's pictures and put them where they belong.

    Runs once, after the gate has approved — never during a revision round,
    where every picture would be paid for and then thrown away.

    Returns the body with images in place, the lead image's URL, and how many
    pictures were actually generated — the last one so the run can be costed,
    since these calls do not pass through the runner. All of it degrades
    quietly: a failed picture leaves an article with one fewer, which is a far
    better outcome than no article.
    """
    if not settings.images.enabled:
        return replace_markers(draft.body, []), "", 0

    site = get_site()
    generator = ImageGenerator(settings.images)
    stem = draft.slug or "article"
    generated = 0

    featured_url = ""
    if draft.featured_image_prompt:
        image = generator.generate(
            ImageRequest(draft.featured_image_prompt, draft.featured_image_alt)
        )
        if image:
            generated += 1
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
    return replace_markers(draft.body, urls), featured_url, generated


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

    illustrated_body, featured_url, images_made = illustrate(draft)

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
        state={"images_generated": images_made},
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
    return LlmAgent(
        name="fact_builder",
        model=_model(settings.models.worker),
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
            (open_article, {"ok": researcher, "no_topic": abort_run}),
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
