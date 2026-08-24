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
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.models import Gemini
from google.adk.tools import google_search
from google.adk.workflow import DEFAULT_ROUTE, JoinNode, Workflow
from google.genai import types

from . import prompts
from .config import settings
from .images import ImageGenerator, ImageRequest, find_markers, replace_markers
from .notify import build_notifier
from .rules import evaluate_reviews
from .schemas import (
    ArticleDraft,
    ResearchBundle,
    ReviewResult,
    RevisionDirective,
)
from .site_client import SiteClient
from .store import Store

logger = logging.getLogger(__name__)

REVIEWER_NODES = ("technical_fact_reviewer", "product_reviewer", "seo_editorial_reviewer")

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


# --------------------------------------------------------------------- nodes


def load_run_context(ctx: Context, node_input: Any) -> Event:
    """Pick today's topic and pull the site's own data before any model runs.

    The catalogue and the archive are fetched here, deterministically, rather
    than handed to an agent as tools: a reviewer that can be told what a product
    costs cannot hallucinate what it costs, and the search-grounded researcher
    keeps its one built-in tool.
    """
    store = get_store()
    site = get_site()

    topic = store.next_topic()
    if topic is None:
        return Event(
            output={"status": "no_topic"},
            route="no_topic",
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="The topic queue is empty.")],
            ),
        )

    article_id = store.start_article(topic["id"])
    store.mark_topic_used(topic["id"])

    products = site.products()
    articles = site.published_articles()
    recent = store.recent_titles()
    previous = store.articles_for_topic(topic["id"])

    logger.info(
        "Run started: topic=%r (written %s time(s) before) article_id=%s "
        "products=%s published=%s",
        topic["title"],
        len(previous),
        article_id,
        len(products),
        len(articles),
    )

    return Event(
        output={"topic": topic["title"], "article_id": article_id},
        state={
            "article_id": article_id,
            "topic_id": topic["id"],
            "topic_title": topic["title"],
            "topic_notes": topic["notes"],
            "topic_keywords": topic["keywords"],
            "site_products": _as_json(products) if products else "(none available)",
            "site_articles": _as_json(
                [a.get("title", "") for a in articles] + recent
            ),
            "previous_on_this_topic": (
                _as_json(previous) if previous else "(nothing yet — this is the first)"
            ),
            "round_number": 0,
            "revision_directive": "",
        },
    )


def persist_registry(ctx: Context, node_input: Any) -> Event:
    """Store the fact registry, and refuse to write from an empty one."""
    raw = ctx.state.get("research_bundle") or {}
    bundle = ResearchBundle(**raw) if raw else ResearchBundle(angle="", outline=[])
    allowed = bundle.allowed_facts

    article_id = int(ctx.state.get("article_id", 0))
    if article_id:
        get_store().save_facts(
            article_id, [f.model_dump() for f in bundle.facts]
        )

    logger.info(
        "Fact registry: %s registered, %s usable.", len(bundle.facts), len(allowed)
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


def evaluate_gate(ctx: Context, node_input: dict[str, Any]) -> Event:
    """The gate: plain code, one verdict, always the same answer for the same input.

    `node_input` arrives from the join as {reviewer_node_name: review_dict}.
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

    decision = evaluate_reviews(
        draft=draft,
        bundle=bundle,
        reviews=reviews,
        round_number=round_number,
        config=settings.quality,
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
        "Round %s verdict: %s (avg %s) - %s",
        round_number,
        decision.verdict,
        decision.average_score,
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
        },
    )


def illustrate(draft: ArticleDraft) -> tuple[str, str]:
    """Make the article's pictures and put them where they belong.

    Runs once, after the gate has approved — never during a revision round,
    where every picture would be paid for and then thrown away.

    Returns the body with images in place, and the lead image's URL. Both
    degrade quietly: a failed picture leaves an article with one fewer, which
    is a far better outcome than no article.
    """
    if not settings.images.enabled:
        return replace_markers(draft.body, []), ""

    site = get_site()
    generator = ImageGenerator(settings.images)
    stem = draft.slug or "article"

    featured_url = ""
    if draft.featured_image_prompt:
        image = generator.generate(
            ImageRequest(draft.featured_image_prompt, draft.featured_image_alt)
        )
        if image:
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
    return replace_markers(draft.body, urls), featured_url


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

    illustrated_body, featured_url = illustrate(draft)

    ok, remote = get_site().create_post(
        title=draft.title,
        slug=draft.slug,
        excerpt=draft.excerpt,
        body=illustrated_body,
        status="draft",
        featured_image=featured_url,
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
        content=types.Content(role="model", parts=[types.Part.from_text(text=message)]),
    )


def abort_run(ctx: Context, node_input: dict[str, Any]) -> Event:
    """Nothing to write, or nothing worth writing from. Say so and stop."""
    status = str((node_input or {}).get("status", "aborted"))
    reasons = {
        "no_topic": "No queued topic — add one before the next run.",
        "no_facts": "Research found no source solid enough to write from.",
    }
    reason = reasons.get(status, "The run stopped early.")

    article_id = int(ctx.state.get("article_id", 0))
    topic_id = int(ctx.state.get("topic_id", 0))
    if article_id:
        get_store().fail_article(article_id, reason)
    if topic_id:
        get_store().release_topic(topic_id)

    if status != "no_topic":
        build_notifier(settings.notify).send(f"Today's run stopped: {reason}")
    logger.warning("Run aborted (%s): %s", status, reason)
    return Event(
        output={"status": status},
        content=types.Content(role="model", parts=[types.Part.from_text(text=reason)]),
    )


# -------------------------------------------------------------------- agents


def build_researcher() -> LlmAgent:
    return LlmAgent(
        name="researcher",
        model=_model(settings.models.worker),
        instruction=prompts.researcher_instruction(settings.content),
        tools=[google_search],
        output_key="research_notes",
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
        instruction=prompts.writer_instruction(settings.content, settings.images),
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
            (load_run_context, {DEFAULT_ROUTE: researcher, "no_topic": abort_run}),
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
