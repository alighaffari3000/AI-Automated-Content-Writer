"""The gate.

This is the one place that decides whether a draft ships, and it is plain code
on purpose: the same reviews always produce the same verdict, and the reason is
readable afterwards. Language models only score and describe — they never
approve.
"""

from __future__ import annotations

from .config import QualityConfig, SafetyConfig
from .normalize import normalize_text
from .schemas import (
    ArticleDraft,
    GateDecision,
    ResearchBundle,
    ReviewIssue,
    ReviewResult,
)

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


def must_reach_a_person(
    draft: ArticleDraft, bundle: ResearchBundle, config: SafetyConfig
) -> list[str]:
    """Reasons this article needs human judgement whatever its score.

    None of these say the draft is wrong. They say that being wrong here would
    cost a reader something the pipeline cannot give back, and that no
    arrangement of reviewers is a substitute for someone taking responsibility.

    Today every article reaches a person anyway, so this changes a headline.
    It is written now because the day autonomy arrives is the wrong day to
    start deciding which articles it does not apply to.
    """
    if not config.enabled:
        return []

    used = {f.fact_id for f in bundle.facts} & set(draft.used_fact_ids)
    facts = [f for f in bundle.facts if f.fact_id in used]
    reasons: list[str] = []

    perishable = sorted({f.kind for f in facts if f.kind in config.fact_kinds})
    if perishable:
        reasons.append(
            "it states "
            + " and ".join(perishable)
            + (", which goes" if len(perishable) == 1 else ", which go")
            + " stale in days"
        )

    unverified = [f.fact_id for f in facts if not f.verified and f.confidence == "LOW"]
    if unverified:
        reasons.append(
            f"{len(unverified)} claim(s) could not be verified at their source: "
            + ", ".join(sorted(unverified))
        )

    if config.terms:
        haystack = normalize_text(f"{draft.title} {draft.excerpt} {draft.body}")
        found = sorted(
            {t for t in config.terms if normalize_text(t) and normalize_text(t) in haystack}
        )
        if found:
            reasons.append("it touches " + ", ".join(found))

    return reasons


def evaluate_reviews(
    draft: ArticleDraft,
    bundle: ResearchBundle,
    reviews: list[ReviewResult],
    round_number: int,
    config: QualityConfig,
    measured: list[ReviewIssue] | None = None,
    safety: SafetyConfig | None = None,
) -> GateDecision:
    """Turn independent reviews into one verdict.

    Returns APPROVE only when every mandatory check passes. Anything else is
    REVISE while rounds remain, and ESCALATE once they run out — the pipeline
    never downgrades a failing draft into a published one.

    `measured` carries the findings that were counted rather than judged — the
    citation check's neighbours from `seo.py`. They are graded by severity and
    not by score, because a title of 78 characters is not a matter of degree:
    critical blocks the draft, major sends it back, and minor travels with the
    decision so the next round can fix it without ever being the reason a draft
    was held.

    `safety` names the subjects no score can clear. A draft that passes every
    check and touches one of them is not approved — it is escalated, because
    APPROVE is the word an automatic publisher will one day read, and these
    articles must never be the ones it reads it on.
    """
    measured = measured or []
    escalate_to_human = must_reach_a_person(draft, bundle, safety or SafetyConfig())

    if not reviews:
        return GateDecision(
            verdict="ESCALATE",
            average_score=0.0,
            reason="No reviews were produced; refusing to pass the draft through.",
            round_number=round_number,
            measured_issues=measured,
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

    measured_critical = [i for i in measured if i.severity == "critical"]
    if measured_critical:
        blocking.extend(i.issue_id for i in measured_critical)
        reasons.append(
            f"{len(measured_critical)} measured defect(s) block publication: "
            + ", ".join(i.issue_id for i in measured_critical)
        )
    measured_major = [i for i in measured if i.severity == "major"]
    if measured_major:
        reasons.append(
            f"{len(measured_major)} measured defect(s) need fixing: "
            + ", ".join(i.issue_id for i in measured_major)
        )

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
        passed = f"All checks passed with an average score of {average}."
        if escalate_to_human:
            return GateDecision(
                verdict="ESCALATE",
                average_score=average,
                reason=(
                    f"{passed} Held for a person because "
                    + "; ".join(escalate_to_human)
                    + "."
                ),
                round_number=round_number,
                measured_issues=measured,
                requires_human=escalate_to_human,
            )
        return GateDecision(
            verdict="APPROVE",
            average_score=average,
            reason=passed,
            round_number=round_number,
            measured_issues=measured,
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
        measured_issues=measured,
        requires_human=escalate_to_human,
    )
