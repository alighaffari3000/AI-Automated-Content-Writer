"""The data contracts every stage of the pipeline speaks.

These are also the LLM output schemas, so a model that answers off-contract is
retried by ADK rather than silently passed downstream.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Confidence = Literal["HIGH", "MEDIUM", "LOW"]
Severity = Literal["critical", "major", "minor"]
Verdict = Literal["APPROVE", "REVISE", "ESCALATE"]


class TopicProposal(BaseModel):
    """The subject of today's article, invented rather than picked off a list.

    A person defines the category; what specifically to write inside it is
    decided per run, against what the site has already published.
    """

    title: str = Field(
        description="The working title, in the article's language. Specific, not a category name."
    )
    angle: str = Field(
        description="What this article argues or answers that a general piece would not."
    )
    keywords: list[str] = Field(
        default_factory=list, description="Search terms this article should answer."
    )
    why_now: str = Field(
        default="",
        description="Why this subject rather than the others available in the category.",
    )


class Fact(BaseModel):
    """One verifiable claim the writer is allowed to use.

    Only claims that can be checked belong here — numbers, specifications,
    comparisons, dates, prices. General explanatory statements need no fact and
    must not be registered, or the registry fills up with truisms.
    """

    fact_id: str = Field(description="Stable id such as FACT-001.")
    claim: str = Field(description="The single verifiable statement.")
    source: str = Field(description="Who published it, e.g. 'Manufacturer datasheet'.")
    source_url: str = Field(default="", description="URL, empty if not web-sourced.")
    evidence: str = Field(description="The passage or data the claim rests on.")
    confidence: Confidence = Field(description="How well the source supports it.")
    allowed: bool = Field(
        default=True,
        description="False when the source is too weak to write from.",
    )


class ResearchBundle(BaseModel):
    """Everything the writer receives: the plan plus the facts it may use."""

    angle: str = Field(description="The specific angle this article takes.")
    outline: list[str] = Field(description="Section headings, in order.")
    target_keywords: list[str] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)

    @property
    def allowed_facts(self) -> list[Fact]:
        return [f for f in self.facts if f.allowed]


class ArticleDraft(BaseModel):
    """A draft. `body` is Markdown; `used_fact_ids` is what makes it auditable."""

    title: str
    slug: str = Field(description="URL-safe ASCII slug, lowercase, hyphens.")
    excerpt: str = Field(description="One-paragraph summary for listings and meta.")
    body: str = Field(
        description=(
            "Full article in Markdown. Mark where a picture belongs with "
            "[[IMAGE: what it shows | alt text]] on its own line."
        )
    )
    featured_image_prompt: str = Field(
        default="",
        description="What the lead image should show, described for an image model.",
    )
    featured_image_alt: str = Field(
        default="",
        description="Alt text for the lead image, in the article's language.",
    )
    category: str = Field(
        default="",
        description="Slug of the site category this belongs in. Must be one that exists.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Two to five tags, reusing the site's existing ones where they fit.",
    )
    used_fact_ids: list[str] = Field(
        default_factory=list,
        description="Every fact_id this draft relies on.",
    )


class ReviewIssue(BaseModel):
    """One defect with an address, so the next round can fix exactly this."""

    issue_id: str = Field(description="Reviewer-prefixed id, e.g. FACT-003, SEO-002.")
    severity: Severity
    location: str = Field(description="Where it is, e.g. 'paragraph 4' or the heading.")
    problem: str
    required_fix: str


class ReviewResult(BaseModel):
    """One reviewer's independent verdict."""

    reviewer: str
    score: float = Field(ge=0, le=10)
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str = Field(default="")

    def has_critical(self) -> bool:
        return any(i.severity == "critical" for i in self.issues)


class GateDecision(BaseModel):
    """The deterministic gate's answer. No model produces this."""

    verdict: Verdict
    average_score: float
    reason: str
    blocking_issue_ids: list[str] = Field(default_factory=list)
    round_number: int = 1


class RevisionDirective(BaseModel):
    """The judge's ordered fix list — the only thing the writer acts on."""

    instructions: list[str] = Field(
        description="Ordered, concrete edits. Each names the issue id it resolves."
    )
    keep_intact: str = Field(
        default="",
        description="What already works and must not be rewritten.",
    )
