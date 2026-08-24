"""The gate.

This is the one place that decides whether a draft ships, and it is plain code
on purpose: the same reviews always produce the same verdict, and the reason is
readable afterwards. Language models only score and describe — they never
approve.
"""

from __future__ import annotations

from .config import QualityConfig
from .schemas import ArticleDraft, GateDecision, ResearchBundle, ReviewResult

SEO_REVIEWER = "seo_editorial"


def unreferenced_fact_ids(
    draft: ArticleDraft, bundle: ResearchBundle
) -> list[str]:
    """Fact ids the draft leans on that the registry does not support.

    Catching this in code rather than asking a reviewer means an invented
    citation can never survive a round, however convincing the prose is.
    """
    allowed = {fact.fact_id for fact in bundle.facts if fact.allowed}
    return sorted({fid for fid in draft.used_fact_ids if fid not in allowed})


def evaluate_reviews(
    draft: ArticleDraft,
    bundle: ResearchBundle,
    reviews: list[ReviewResult],
    round_number: int,
    config: QualityConfig,
) -> GateDecision:
    """Turn independent reviews into one verdict.

    Returns APPROVE only when every mandatory check passes. Anything else is
    REVISE while rounds remain, and ESCALATE once they run out — the pipeline
    never downgrades a failing draft into a published one.
    """
    if not reviews:
        return GateDecision(
            verdict="ESCALATE",
            average_score=0.0,
            reason="No reviews were produced; refusing to pass the draft through.",
            round_number=round_number,
        )

    blocking: list[str] = []
    reasons: list[str] = []

    phantom = unreferenced_fact_ids(draft, bundle)
    if phantom:
        blocking.extend(phantom)
        reasons.append(
            f"{len(phantom)} claim(s) cite facts that are not in the registry: "
            + ", ".join(phantom)
        )

    for review in reviews:
        criticals = [i.issue_id for i in review.issues if i.severity == "critical"]
        if criticals:
            blocking.extend(criticals)
            reasons.append(f"{review.reviewer} raised {len(criticals)} critical issue(s).")

    average = round(sum(r.score for r in reviews) / len(reviews), 2)
    if average < config.min_average_score:
        reasons.append(
            f"Average score {average} is below the bar of {config.min_average_score}."
        )

    seo = next((r for r in reviews if r.reviewer == SEO_REVIEWER), None)
    if seo is not None and seo.score < config.min_seo_score:
        reasons.append(
            f"SEO score {seo.score} is below the bar of {config.min_seo_score}."
        )

    if not reasons:
        return GateDecision(
            verdict="APPROVE",
            average_score=average,
            reason=f"All checks passed with an average score of {average}.",
            round_number=round_number,
        )

    exhausted = round_number >= config.max_revision_rounds
    return GateDecision(
        verdict="ESCALATE" if exhausted else "REVISE",
        average_score=average,
        reason=(
            f"Round {round_number} of {config.max_revision_rounds}. "
            + " ".join(reasons)
            + (" No rounds left, handing it to a human." if exhausted else "")
        ),
        blocking_issue_ids=sorted(set(blocking)),
        round_number=round_number,
    )
