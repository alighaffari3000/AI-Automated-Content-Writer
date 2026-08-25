"""The instrument that measures the reviewers, measured itself.

Running the suite needs a model; everything around it does not. What is pinned
down here is the part that would fail silently: a scenario naming a reviewer
that does not exist can never be caught by anyone, and a summary that computes
its numbers wrongly would report a regression that never happened — or worse,
hide one that did.
"""

from __future__ import annotations

import json

import pytest

from app import evaluation
from app.agent import REVIEWER_NODES
from app.config import settings
from app.cost import RunCost
from app.evaluation import Scenario, ScenarioResult, compare, load_scenarios, summarise
from app.rules import SEO_REVIEWER
from app.schemas import ArticleDraft, ResearchBundle
from app.seo import SiteIndex, defects

REVIEWERS = {name.replace("_reviewer", "") for name in REVIEWER_NODES}

SCENARIOS = load_scenarios()


# ------------------------------------------------------------- the scenarios


def test_the_suite_has_planted_defects_and_a_control():
    assert any(s.is_control for s in SCENARIOS), "without a control, crying wolf is free"
    assert sum(1 for s in SCENARIOS if not s.is_control) >= 5


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_every_scenario_is_a_draft_the_pipeline_could_have_produced(scenario):
    """A scenario that does not parse is a test that never runs."""
    ArticleDraft(**scenario.draft)
    ResearchBundle(**scenario.bundle)
    assert scenario.description


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_every_planted_defect_names_a_reviewer_that_exists(scenario):
    """A typo here makes a scenario unfalsifiable: nobody can ever catch it."""
    if scenario.is_control or scenario.carried_by_gate:
        return
    assert scenario.expect_reviewers, "a planted defect nobody is expected to catch"
    assert set(scenario.expect_reviewers) <= REVIEWERS
    assert SEO_REVIEWER in REVIEWERS, "and the gate's own name agrees with them"


@pytest.mark.parametrize(
    "scenario",
    [s for s in SCENARIOS if s.carried_by_gate],
    ids=lambda s: s.id,
)
def test_a_gate_carried_scenario_really_is_measurable(scenario):
    """It must fail in code, or it is asking the reviewers to stay quiet about
    a defect that nothing else catches."""
    draft = ArticleDraft(**scenario.draft)
    bundle = ResearchBundle(**scenario.bundle)
    found = defects(draft, SiteIndex(), bundle.target_keywords, settings.seo)
    assert [i for i in found if i.severity in ("major", "critical")], (
        "nothing measurable is wrong with this draft"
    )


@pytest.mark.parametrize(
    "scenario",
    [s for s in SCENARIOS if s.is_control],
    ids=lambda s: s.id,
)
def test_a_control_carries_no_measurable_defect_either(scenario):
    """Otherwise the control fails for a reason that has nothing to do with the
    reviewers it exists to test."""
    draft = ArticleDraft(**scenario.draft)
    bundle = ResearchBundle(**scenario.bundle)
    found = defects(draft, SiteIndex(), bundle.target_keywords, settings.seo)
    assert not [i for i in found if i.severity in ("major", "critical")]


# ---------------------------------------------------------------- the numbers


def result(name: str, passed: bool, score: float, control: bool = False) -> ScenarioResult:
    return ScenarioResult(
        scenario=Scenario(
            id=name, description="a scenario", bundle={}, draft={}, is_control=control
        ),
        verdict="APPROVE" if control else "REVISE",
        caught_by=["technical_fact"] if passed else [],
        scores={"technical_fact": score},
        passed=passed,
        reason="caught" if passed else "missed",
    )


def test_the_detection_rate_counts_only_planted_defects():
    summary = summarise(
        [
            result("a", True, 7.0),
            result("b", False, 8.0),
            result("control", True, 9.0, control=True),
        ],
        RunCost(),
    )
    assert summary["detection_rate"] == 0.5
    assert summary["controls_clean"] == 1.0


def test_separation_is_what_a_clean_draft_earns_over_a_defective_one():
    summary = summarise(
        [result("a", True, 7.0), result("control", True, 9.0, control=True)], RunCost()
    )
    assert summary["separation"] == 2.0


def test_a_board_that_scores_everything_the_same_shows_no_separation():
    """The failure that started this: high scores, nothing found, and a
    detection rate that hides it."""
    summary = summarise(
        [result("a", True, 9.5), result("control", True, 9.5, control=True)], RunCost()
    )
    assert summary["detection_rate"] == 1.0
    assert summary["separation"] == 0.0


def test_repeated_attempts_are_aggregated_per_scenario():
    summary = summarise(
        [result("a", True, 8.0), result("a", False, 7.0), result("a", True, 9.0)],
        RunCost(),
    )
    entry = summary["scenarios"]["a"]
    assert (entry["attempts"], entry["passes"]) == (3, 2)
    assert entry["mean_score"] == 8.0


# ------------------------------------------------------------- the comparison


def test_a_drop_in_detection_is_reported_as_a_drop():
    now = summarise([result("a", False, 8.0)], RunCost())
    earlier = summarise([result("a", True, 8.0)], RunCost())
    lines = compare(now, earlier)
    assert any("▼" in line and "defects caught" in line for line in lines)
    assert any("a: 100% → 0%" in line for line in lines)


def test_a_run_that_did_not_move_says_so():
    now = summarise([result("a", True, 8.0)], RunCost())
    assert compare(now, dict(now)) == []


def test_a_new_scenario_is_named_rather_than_read_as_a_regression():
    now = summarise([result("a", True, 8.0), result("b", True, 8.0)], RunCost())
    earlier = summarise([result("a", True, 8.0)], RunCost())
    assert any("+ b: new scenario" in line for line in compare(now, earlier))


def test_a_run_is_kept_so_the_next_one_has_something_to_compare_against(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(evaluation, "results_dir", lambda: tmp_path)
    summary = summarise([result("a", True, 8.0)], RunCost())
    path = evaluation.save(summary)

    assert json.loads(path.read_text(encoding="utf-8"))["detection_rate"] == 1.0
    assert evaluation.previous()["at"] == summary["at"]
    assert evaluation.previous(before=path) is None, "a run does not compare to itself"
