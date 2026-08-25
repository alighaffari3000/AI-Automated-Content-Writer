"""Does the review board actually catch anything?

The pipeline's whole claim is that a bad draft does not get through. That
claim was never measured — the reviewers scored 10/10/9.5 on their first
outing and found nothing, which looked like quality and was actually a broken
instrument.

So this does not ask "is the article good", which no fixed answer key can
settle. It hands the reviewers drafts with known defects planted in them and
asks a narrower question with a right answer: *was the defect found, and was
it rated as seriously as it deserves?*

Three numbers come out of it, and the third is the one that was missing:

  detection rate    how many planted defects were caught.
  controls clean    how often a sound draft was called unpublishable — a
                    reviewer that cries wolf is as useless as one that sleeps.
  separation        the gap between what a clean draft scores and what a
                    defective one scores. This is the calibration number. A
                    board that scores everything 9 has a detection rate that
                    looks fine and a separation of nothing, and it is not
                    reviewing, it is applauding.

Model output varies, so one pass is a sample rather than a measurement: use
`--repeat` when a number is going to be compared against another number. Every
run is saved, and the next one prints the difference — which is what makes this
a regression test for prompts rather than a thing to look at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    # A defect the gate measures rather than the reviewers judge. Passing means
    # the gate caught it *and* no reviewer spent a critical on it: since the
    # measurements moved into code, a reviewer arguing about title length is
    # a reviewer not doing the job only it can do.
    carried_by_gate: bool = False


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

    criticals = [
        r.reviewer for r in reviews if any(i.severity == "critical" for i in r.issues)
    ]

    if scenario.carried_by_gate:
        verdict_ok = decision.verdict == scenario.expect_verdict
        passed = verdict_ok and not criticals
        reason = (
            "carried by the gate"
            if passed
            else "; ".join(
                filter(
                    None,
                    [
                        f"gate said {decision.verdict}, expected {scenario.expect_verdict}"
                        if not verdict_ok
                        else "",
                        f"{criticals} spent a critical on something already measured"
                        if criticals
                        else "",
                    ],
                )
            )
        )
    elif scenario.is_control:
        # A clean draft must not be savaged. Wanting improvements is fine;
        # calling it unpublishable is a false positive, and a reviewer that
        # cries wolf is as useless as one that sleeps.
        passed = not criticals
        reason = "clean draft passed" if passed else f"false critical from {criticals}"
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


async def run_all(
    scenarios: list[Scenario], repeat: int = 1
) -> tuple[list[ScenarioResult], RunCost]:
    cost = RunCost()
    results = []
    for attempt in range(1, repeat + 1):
        for scenario in scenarios:
            logger.info(
                "scenario %s (attempt %s/%s) — %s",
                scenario.id,
                attempt,
                repeat,
                scenario.description,
            )
            results.append(await run_scenario(scenario, cost))
    return results, cost


# ------------------------------------------------------------------ scoring


def mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def summarise(results: list[ScenarioResult], cost: RunCost) -> dict[str, Any]:
    """The whole run as numbers, in the shape the next run will compare against."""
    planted = [r for r in results if not r.scenario.is_control]
    controls = [r for r in results if r.scenario.is_control]
    planted_scores = [s for r in planted for s in r.scores.values()]
    control_scores = [s for r in controls for s in r.scores.values()]

    by_scenario: dict[str, Any] = {}
    for result in results:
        entry = by_scenario.setdefault(
            result.scenario.id,
            {
                "description": result.scenario.description,
                "control": result.scenario.is_control,
                "attempts": 0,
                "passes": 0,
                "scores": [],
                "verdicts": [],
                "reasons": [],
            },
        )
        entry["attempts"] += 1
        entry["passes"] += int(result.passed)
        entry["scores"].extend(result.scores.values())
        entry["verdicts"].append(result.verdict)
        if not result.passed:
            entry["reasons"].append(result.reason)
    for entry in by_scenario.values():
        entry["mean_score"] = mean(entry.pop("scores"))

    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "models": {
            "worker": settings.models.worker,
            "author": settings.models.author,
        },
        "detection_rate": (
            round(sum(1 for r in planted if r.passed) / len(planted), 3)
            if planted
            else 0.0
        ),
        "controls_clean": (
            round(sum(1 for r in controls if r.passed) / len(controls), 3)
            if controls
            else 0.0
        ),
        "mean_planted_score": mean(planted_scores),
        "mean_control_score": mean(control_scores),
        # What a clean draft earns over a defective one. The calibration
        # number: near zero means the board is not discriminating, however
        # many defects it technically flagged.
        "separation": round(mean(control_scores) - mean(planted_scores), 2),
        "tokens": cost.total_input + cost.total_output,
        "usd": round(
            cost.estimate_usd(settings.cost.rates, settings.cost.image_usd), 4
        ),
        "scenarios": by_scenario,
    }


def results_dir() -> Path:
    return Path(settings.db_path).resolve().parent / "eval"


def save(summary: dict[str, Any]) -> Path:
    """Keep every run. Two numbers are a trend; one is an anecdote."""
    directory = results_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = summary["at"].replace(":", "").replace("-", "")
    path = directory / f"eval-{stamp}.json"
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def previous(before: Path | None = None) -> dict[str, Any] | None:
    """The last saved run, for comparison."""
    directory = results_dir()
    if not directory.exists():
        return None
    files = sorted(p for p in directory.glob("eval-*.json") if p != before)
    for path in reversed(files):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def compare(summary: dict[str, Any], earlier: dict[str, Any]) -> list[str]:
    """What moved since the run before, and in which direction."""
    lines = []
    for key, label, digits in (
        ("detection_rate", "defects caught", 3),
        ("controls_clean", "controls clean", 3),
        ("separation", "separation", 2),
        ("mean_planted_score", "mean score, planted", 2),
        ("mean_control_score", "mean score, control", 2),
    ):
        was, now = earlier.get(key), summary.get(key)
        if was is None or now is None or round(was - now, 4) == 0:
            continue
        arrow = "▲" if now > was else "▼"
        lines.append(f"  {arrow} {label}: {was:.{digits}f} → {now:.{digits}f}")

    for name, entry in summary["scenarios"].items():
        old = (earlier.get("scenarios") or {}).get(name)
        if not old:
            lines.append(f"  + {name}: new scenario")
            continue
        was = old["passes"] / max(old["attempts"], 1)
        now = entry["passes"] / max(entry["attempts"], 1)
        if round(was - now, 3) != 0:
            lines.append(f"  · {name}: {was:.0%} → {now:.0%} of attempts passed")
    return lines


def report(results: list[ScenarioResult], summary: dict[str, Any]) -> int:
    """Print the scorecard. Returns the number of scenarios that did not hold."""
    print()
    for name, entry in summary["scenarios"].items():
        held = entry["passes"] == entry["attempts"]
        mark = "PASS" if held else "FAIL"
        kind = "control" if entry["control"] else "planted"
        rate = (
            "" if entry["attempts"] == 1 else f" ({entry['passes']}/{entry['attempts']})"
        )
        print(f"[{mark}]{rate} {name} ({kind}) — {entry['description']}")
        print(f"       mean score {entry['mean_score']}, gate {'/'.join(dict.fromkeys(entry['verdicts']))}")
        for reason in dict.fromkeys(entry["reasons"]):
            print(f"       ↳ {reason}")

    print()
    print(f"defects caught : {summary['detection_rate']:.0%}")
    print(f"controls clean : {summary['controls_clean']:.0%}")
    print(
        f"separation     : {summary['separation']:+.2f} "
        f"(control {summary['mean_control_score']} vs planted "
        f"{summary['mean_planted_score']})"
    )
    print(f"cost           : {summary['tokens']:,} tokens, ~${summary['usd']:.3f}")
    return sum(
        1 for e in summary["scenarios"].values() if e["passes"] != e["attempts"]
    )


def main(repeat: int = 1, keep: bool = True) -> int:
    scenarios = load_scenarios()
    results, cost = asyncio.run(run_all(scenarios, repeat=repeat))
    summary = summarise(results, cost)
    failures = report(results, summary)

    earlier = previous()
    if keep:
        path = save(summary)
        print(f"saved          : {path}")
    if earlier:
        changes = compare(summary, earlier)
        print(f"\nsince {earlier['at']}:")
        print("\n".join(changes) if changes else "  nothing moved")
    return failures
