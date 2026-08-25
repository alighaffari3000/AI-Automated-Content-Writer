"""The gate decides what ships, so its behaviour is pinned down here.

These are deterministic checks on plain code — no model is called. How well the
agents actually write is a question for eval, not for pytest.
"""

from __future__ import annotations

import pytest

from app.config import QualityConfig, SafetyConfig
from app.rules import evaluate_reviews, unreferenced_fact_ids
from app.schemas import (
    ArticleDraft,
    Fact,
    GateDecision,
    ResearchBundle,
    ReviewIssue,
    ReviewResult,
)


def measured(severity: str, issue_id: str = "SEO-TITLE-LONG") -> ReviewIssue:
    """One finding of the kind the gate counts rather than judges."""
    return ReviewIssue(
        issue_id=issue_id,
        severity=severity,  # type: ignore[arg-type]
        location="seo_title",
        problem="a measurement",
        required_fix="a fix",
    )


CONFIG = QualityConfig(max_revision_rounds=3, min_average_score=8.0, min_seo_score=7.0)


def make_bundle(*fact_ids: str, disallowed: tuple[str, ...] = ()) -> ResearchBundle:
    return ResearchBundle(
        angle="an angle",
        outline=["one"],
        facts=[
            Fact(
                fact_id=fid,
                claim="a claim",
                source="a source",
                evidence="a passage",
                confidence="HIGH",
                allowed=fid not in disallowed,
            )
            for fid in fact_ids
        ],
    )


def make_draft(*used: str) -> ArticleDraft:
    return ArticleDraft(
        title="A title",
        slug="a-title",
        excerpt="An excerpt.",
        body="Body.",
        used_fact_ids=list(used),
    )


def review(name: str, score: float, *severities: str) -> ReviewResult:
    return ReviewResult(
        reviewer=name,
        score=score,
        issues=[
            ReviewIssue(
                issue_id=f"{name.upper()}-{i}",
                severity=sev,  # type: ignore[arg-type]
                location="paragraph 1",
                problem="a problem",
                required_fix="a fix",
            )
            for i, sev in enumerate(severities, start=1)
        ],
    )


def clean_reviews(score: float = 9.0) -> list[ReviewResult]:
    return [
        review("technical_fact", score),
        review("product", score),
        review("seo_editorial", score),
    ]


def test_approves_when_every_check_passes():
    decision = evaluate_reviews(
        draft=make_draft("FACT-001"),
        bundle=make_bundle("FACT-001"),
        reviews=clean_reviews(),
        round_number=1,
        config=CONFIG,
    )
    assert decision.verdict == "APPROVE"
    assert decision.average_score == 9.0


def test_a_single_critical_issue_blocks_a_high_scoring_draft():
    reviews = clean_reviews(10.0)
    reviews[0] = review("technical_fact", 10.0, "critical")
    decision = evaluate_reviews(
        draft=make_draft("FACT-001"),
        bundle=make_bundle("FACT-001"),
        reviews=reviews,
        round_number=1,
        config=CONFIG,
    )
    assert decision.verdict == "REVISE"
    assert "TECHNICAL_FACT-1" in decision.blocking_issue_ids


def test_a_citation_the_registry_does_not_back_is_caught_in_code():
    """No reviewer has to notice this — the gate checks it directly."""
    decision = evaluate_reviews(
        draft=make_draft("FACT-001", "FACT-999"),
        bundle=make_bundle("FACT-001"),
        reviews=clean_reviews(10.0),
        round_number=1,
        config=CONFIG,
    )
    assert decision.verdict == "REVISE"
    assert decision.blocking_issue_ids == ["FACT-999"]


def test_a_fact_marked_unusable_cannot_be_cited():
    decision = evaluate_reviews(
        draft=make_draft("FACT-002"),
        bundle=make_bundle("FACT-001", "FACT-002", disallowed=("FACT-002",)),
        reviews=clean_reviews(10.0),
        round_number=1,
        config=CONFIG,
    )
    assert decision.verdict == "REVISE"
    assert decision.blocking_issue_ids == ["FACT-002"]


@pytest.mark.parametrize(
    ("scores", "expected"),
    [((9.0, 9.0, 9.0), "APPROVE"), ((8.0, 8.0, 8.0), "APPROVE"), ((7.9, 8.0, 8.0), "REVISE")],
)
def test_the_average_score_bar(scores, expected):
    reviews = [
        review("technical_fact", scores[0]),
        review("product", scores[1]),
        review("seo_editorial", scores[2]),
    ]
    decision = evaluate_reviews(
        draft=make_draft(),
        bundle=make_bundle(),
        reviews=reviews,
        round_number=1,
        config=CONFIG,
    )
    assert decision.verdict == expected


def test_weak_seo_alone_sends_it_back_even_with_a_passing_average():
    reviews = [
        review("technical_fact", 10.0),
        review("product", 10.0),
        review("seo_editorial", 6.5),
    ]
    decision = evaluate_reviews(
        draft=make_draft(),
        bundle=make_bundle(),
        reviews=reviews,
        round_number=1,
        config=CONFIG,
    )
    assert decision.verdict == "REVISE"


def test_the_last_round_escalates_instead_of_looping():
    decision = evaluate_reviews(
        draft=make_draft(),
        bundle=make_bundle(),
        reviews=clean_reviews(5.0),
        round_number=3,
        config=CONFIG,
    )
    assert decision.verdict == "ESCALATE"
    assert "No rounds left" in decision.reason


def test_missing_reviews_never_read_as_approval():
    decision = evaluate_reviews(
        draft=make_draft(),
        bundle=make_bundle(),
        reviews=[],
        round_number=1,
        config=CONFIG,
    )
    assert decision.verdict == "ESCALATE"


def test_a_measured_critical_blocks_a_draft_every_reviewer_liked():
    """A slug collision or a link into a 404 needs no reviewer to notice it."""
    decision = evaluate_reviews(
        draft=make_draft(),
        bundle=make_bundle(),
        reviews=clean_reviews(10.0),
        round_number=1,
        config=CONFIG,
        measured=[measured("critical", "SEO-SLUG-TAKEN")],
    )
    assert decision.verdict == "REVISE"
    assert decision.blocking_issue_ids == ["SEO-SLUG-TAKEN"]


def test_a_measured_major_sends_the_draft_back_without_blocking_it():
    decision = evaluate_reviews(
        draft=make_draft(),
        bundle=make_bundle(),
        reviews=clean_reviews(10.0),
        round_number=1,
        config=CONFIG,
        measured=[measured("major")],
    )
    assert decision.verdict == "REVISE"
    assert decision.blocking_issue_ids == []
    assert "SEO-TITLE-LONG" in decision.reason


def test_a_measured_minor_never_costs_a_draft_a_round():
    """Polish travels with the decision; it is not a reason to hold an article."""
    decision = evaluate_reviews(
        draft=make_draft(),
        bundle=make_bundle(),
        reviews=clean_reviews(10.0),
        round_number=1,
        config=CONFIG,
        measured=[measured("minor")],
    )
    assert decision.verdict == "APPROVE"
    assert [i.issue_id for i in decision.measured_issues] == ["SEO-TITLE-LONG"]


def test_measurements_reach_the_judge_even_when_the_run_is_escalating():
    decision = evaluate_reviews(
        draft=make_draft(),
        bundle=make_bundle(),
        reviews=[],
        round_number=1,
        config=CONFIG,
        measured=[measured("critical", "SEO-LINK-BROKEN-1")],
    )
    assert decision.verdict == "ESCALATE"
    assert [i.issue_id for i in decision.measured_issues] == ["SEO-LINK-BROKEN-1"]


SAFETY = SafetyConfig(enabled=True, terms=("subsidy", "یارانه"), fact_kinds=("price",))


def priced_bundle(kind: str = "price", **overrides) -> ResearchBundle:
    fields = {
        "fact_id": "FACT-001",
        "claim": "a claim",
        "source": "a source",
        "evidence": "a passage",
        "confidence": "HIGH",
        "kind": kind,
        "verified": True,
    }
    fields.update(overrides)
    return ResearchBundle(angle="an angle", outline=["one"], facts=[Fact(**fields)])


def gate(draft: ArticleDraft, bundle: ResearchBundle, safety=SAFETY) -> GateDecision:
    return evaluate_reviews(
        draft=draft,
        bundle=bundle,
        reviews=clean_reviews(9.0),
        round_number=1,
        config=CONFIG,
        safety=safety,
    )


def test_a_flawless_article_about_prices_is_still_held_for_a_person():
    """Nothing is wrong with it. Being wrong about a price is not recoverable."""
    decision = gate(make_draft("FACT-001"), priced_bundle())
    assert decision.verdict == "ESCALATE"
    assert "goes stale in days" in decision.reason
    assert decision.requires_human


def test_a_subject_a_person_must_see_is_recognised_in_the_site_s_own_language():
    draft = ArticleDraft(
        title="یارانه انرژی خورشیدی",
        slug="solar-subsidy",
        excerpt="یک خلاصه.",
        body="متن دربارهٔ یارانه‌ها.",  # noqa: RUF001
        used_fact_ids=["FACT-001"],
    )
    decision = gate(draft, priced_bundle(kind="general"))
    assert decision.verdict == "ESCALATE"
    assert "یارانه" in decision.reason


def test_a_claim_nobody_could_verify_reaches_a_person():
    bundle = priced_bundle(kind="general", verified=False, confidence="LOW")
    decision = gate(make_draft("FACT-001"), bundle)
    assert decision.verdict == "ESCALATE"
    assert "could not be verified" in decision.reason


def test_an_ordinary_article_is_approved_as_before():
    decision = gate(make_draft("FACT-001"), priced_bundle(kind="specification"))
    assert decision.verdict == "APPROVE"
    assert decision.requires_human == []


def test_a_fact_the_draft_never_used_does_not_hold_it_back():
    """The registry may hold a price the article decided not to state."""
    decision = gate(make_draft(), priced_bundle())
    assert decision.verdict == "APPROVE"


def test_the_safety_gate_can_be_turned_off():
    decision = gate(
        make_draft("FACT-001"), priced_bundle(), safety=SafetyConfig(enabled=False)
    )
    assert decision.verdict == "APPROVE"


def test_a_held_article_that_also_failed_a_check_still_says_both():
    decision = evaluate_reviews(
        draft=make_draft("FACT-001", "FACT-999"),
        bundle=priced_bundle(),
        reviews=clean_reviews(9.0),
        round_number=1,
        config=CONFIG,
        safety=SAFETY,
    )
    assert decision.verdict == "REVISE", "a defect is fixed before a person reads it"
    assert decision.requires_human, "and the reason it needs one travels with it"


def test_unreferenced_fact_ids_reports_each_id_once():
    assert unreferenced_fact_ids(
        make_draft("FACT-9", "FACT-9", "FACT-1"), make_bundle("FACT-1")
    ) == ["FACT-9"]
