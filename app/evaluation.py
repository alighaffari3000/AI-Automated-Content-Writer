"""Does the review board actually catch anything?

The pipeline's whole claim is that a bad draft does not get through. That
claim was never measured — the reviewers scored 10/10/9.5 on their first
outing and found nothing, which looked like quality and was actually a broken
instrument.

So this does not ask "is the article good", which no fixed answer key can
settle. It hands the reviewers drafts with known defects planted in them and
asks a narrower question with a right answer: *was the defect found, and was
it rated as seriously as it deserves?*

Run it after any change to the review prompts, the models or the gate. A
detection rate that drops is a regression, whatever the article looks like.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.adk.workflow import JoinNode, Workflow
from google.genai import types

from . import agent as pipeline
from . import seo
from .config import settings
from .cost import RunCost, usage_from_event
from .rules import evaluate_reviews
from .schemas import ArticleDraft, ResearchBundle, ReviewResult

logger = logging.getLogger(__name__)

SCENARIOS_PATH = Path(__file__).resolve().parent.parent / "tests" / "eval" / "scenarios.json"


@dataclass
class Scenario:
    """One draft with a known defect, and what a working reviewer should say."""

    id: str
    description: str
    bundle: dict[str, Any]
    draft: dict[str, Any]
    site_products: str = "(none available)"
    expect_verdict: str = "REVISE"
    expect_reviewers: list[str] = field(default_factory=list)
    expect_min_severity: str = "major"
    # A clean control: nothing planted, and flagging it critical is the failure.
    is_control: bool = False


@dataclass
class ScenarioResult:
    scenario: Scenario
    verdict: str
    caught_by: list[str]
    scores: dict[str, float]
    passed: bool
    reason: str


SEVERITY_RANK = {"minor": 1, "major": 2, "critical": 3}


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[Scenario]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Scenario(**entry) for entry in data]


def build_review_only_graph() -> Workflow:
    """Just the review board and the gate — no research, no writing, no publish.

    Reusing the real reviewer agents is the point: an eval against a copy of
    the prompts would pass while production failed.
    """
    technical, product, editorial = pipeline.build_reviewers()
    join = JoinNode(name="join_reviews")

    def start(node_input: Any) -> dict[str, str]:
        return {"ok": "review"}

    def collect(node_input: dict[str, Any]) -> dict[str, Any]:
        return node_input

    return Workflow(
        name="review_board_eval",
        edges=[
            ("START", start),
            (start, (technical, product, editorial)),
            ((technical, product, editorial), join),
            (join, collect),
        ],
    )


async def run_scenario(scenario: Scenario, cost: RunCost) -> ScenarioResult:
    """Put one planted draft in front of the real reviewers."""
    app = App(root_agent=build_review_only_graph(), name="review_eval")
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="review_eval",
        user_id="eval",
        session_id=f"eval-{scenario.id}",
        state={
            "draft": scenario.draft,
            "research_bundle": scenario.bundle,
            "site_products": scenario.site_products,
            "site_articles": "[]",
            "sources_for_prompt": "(not part of this scenario)",
            "target_keywords": ", ".join(scenario.bundle.get("target_keywords", [])),
        },
    )

    joined: dict[str, Any] = {}
    async for event in runner.run_async(
        user_id="eval",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text="review")]),
    ):
        usage = usage_from_event(event, pipeline.agent_models())
        if usage:
            cost.record_tokens(*usage)
        if isinstance(getattr(event, "output", None), dict) and "product_reviewer" in event.output:
            joined = event.output

    reviews: list[ReviewResult] = []
    for node_name in pipeline.REVIEWER_NODES:
        payload = joined.get(node_name)
        if payload:
            try:
                reviews.append(ReviewResult(**payload))
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s: unusable review from %s: %s", scenario.id, node_name, exc)

    draft = ArticleDraft(**scenario.draft)
    bundle = ResearchBundle(**scenario.bundle)

    # The measurements ride along exactly as they do in a real run, so the
    # verdict here is the verdict the pipeline would reach. They cannot rescue
    # a scenario, though: a defect only counts as caught when the reviewer it
    # was planted for names it. The site index is empty because these drafts
    # belong to no site, which switches off the checks that need one.
    decision = evaluate_reviews(
        draft=draft,
        bundle=bundle,
        reviews=reviews,
        round_number=1,
        config=settings.quality,
        measured=seo.defects(
            draft, seo.SiteIndex(), bundle.target_keywords, settings.seo
        ),
    )

    threshold = SEVERITY_RANK[scenario.expect_min_severity]
    caught = [
        r.reviewer
        for r in reviews
        if any(SEVERITY_RANK.get(i.severity, 0) >= threshold for i in r.issues)
    ]
    scores = {r.reviewer: r.score for r in reviews}

    if scenario.is_control:
        # A clean draft must not be savaged. Wanting improvements is fine;
        # calling it unpublishable is a false positive, and a reviewer that
        # cries wolf is as useless as one that sleeps.
        critical = [
            r.reviewer for r in reviews if any(i.severity == "critical" for i in r.issues)
        ]
        passed = not critical
        reason = "clean draft passed" if passed else f"false critical from {critical}"
    else:
        expected = set(scenario.expect_reviewers)
        found = expected.intersection(caught)
        verdict_ok = decision.verdict == scenario.expect_verdict
        passed = bool(found) and verdict_ok
        missing = expected - set(caught)
        reason = (
            "caught"
            if passed
            else "; ".join(
                filter(
                    None,
                    [
                        f"missed by {sorted(missing)}" if missing else "",
                        f"gate said {decision.verdict}, expected {scenario.expect_verdict}"
                        if not verdict_ok
                        else "",
                    ],
                )
            )
            or "not caught"
        )

    return ScenarioResult(
        scenario=scenario,
        verdict=decision.verdict,
        caught_by=caught,
        scores=scores,
        passed=passed,
        reason=reason,
    )


async def run_all(scenarios: list[Scenario]) -> tuple[list[ScenarioResult], RunCost]:
    cost = RunCost()
    results = []
    for scenario in scenarios:
        logger.info("scenario %s — %s", scenario.id, scenario.description)
        results.append(await run_scenario(scenario, cost))
    return results, cost


def report(results: list[ScenarioResult], cost: RunCost) -> int:
    """Print the scorecard. Returns the number of failures."""
    planted = [r for r in results if not r.scenario.is_control]
    controls = [r for r in results if r.scenario.is_control]
    failures = [r for r in results if not r.passed]

    print()
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        kind = "control" if result.scenario.is_control else "planted"
        scores = " ".join(f"{k.split('_')[0]}={v}" for k, v in sorted(result.scores.items()))
        print(f"[{mark}] {result.scenario.id} ({kind}) — {result.scenario.description}")
        print(f"       gate={result.verdict}  {scores}")
        if not result.passed:
            print(f"       ↳ {result.reason}")

    caught = sum(1 for r in planted if r.passed)
    print()
    print(f"defects caught : {caught}/{len(planted)}")
    if controls:
        clean = sum(1 for r in controls if r.passed)
        print(f"controls clean : {clean}/{len(controls)}")
    all_scores = [s for r in results for s in r.scores.values()]
    if all_scores:
        print(f"mean score     : {sum(all_scores) / len(all_scores):.2f}")
    print(
        f"cost           : {cost.total_input + cost.total_output:,} tokens, "
        f"~${cost.estimate_usd(settings.cost.rates, settings.cost.image_usd):.3f}"
    )
    return len(failures)


def main() -> int:
    scenarios = load_scenarios()
    results, cost = asyncio.run(run_all(scenarios))
    return report(results, cost)
