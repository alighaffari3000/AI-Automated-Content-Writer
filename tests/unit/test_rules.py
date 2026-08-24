"""The gate decides what ships, so its behaviour is pinned down here.

These are deterministic checks on plain code — no model is called. How well the
agents actually write is a question for eval, not for pytest.
"""

from __future__ import annotations

import pytest

from app.config import QualityConfig
from app.rules import evaluate_reviews, unreferenced_fact_ids
from app.schemas import ArticleDraft, Fact, ResearchBundle, ReviewIssue, ReviewResult

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


def test_unreferenced_fact_ids_reports_each_id_once():
    assert unreferenced_fact_ids(
        make_draft("FACT-9", "FACT-9", "FACT-1"), make_bundle("FACT-1")
    ) == ["FACT-9"]
