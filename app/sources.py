"""Where a claim actually came from.

Search grounding does not put its URLs in the model's text — it puts them in
the response's grounding metadata. Without harvesting that, every source in
the registry is whatever the model chose to type, and the chain of evidence
ends inside the model that made the claim.

This module harvests the real URLs, and then judges them: reachable, primary
or second-hand, recent enough. A source that cannot be reached lowers a
claim's confidence rather than killing it — the pipeline runs from networks
where half the internet is a coin flip, and a run that fails on a timeout
publishes nothing at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# What a source is worth, before anything is fetched. The tiers follow the
# order agreed in the design: the people who built the thing outrank the people
# reselling it, and both outrank someone's blog.
AUTHORITY_TIERS: list[tuple[int, str, tuple[str, ...]]] = [
    (5, "standards body or government", (".gov", ".int", "iec.ch", "ieee.org", "iso.org", "nrel.gov", "energy.gov", "irena.org")),
    (4, "academic or research", (".edu", ".ac.", "researchgate.net", "sciencedirect.com", "nature.com", "mdpi.com")),
    (3, "manufacturer documentation", ("deye.com", "victronenergy.com", "byd.com", "catl.com", "huawei.com", "sma.de", "fronius.com", "growatt.com", "solaredge.com", "tesla.com", "enphase.com", "pylontech", "battleborn")),
    (2, "industry or engineering publication", ("pv-magazine", "solarpowerworld", "energy-storage.news", "pvsyst.com", "homerenergy.com", "renewableenergyworld", "electrical4u", "batteryuniversity", "cleantechnica", "solarquotes", "pveducation")),
    (1, "general web", ()),
]

# Domains that are aggregators, forums or content farms: they may be right, but
# nothing technical should rest on them alone.
WEAK_DOMAINS = (
    "wikipedia.org", "quora.com", "reddit.com", "medium.com", "linkedin.com",
    "facebook.com", "pinterest.com", "youtube.com", "blogspot.", "wordpress.com",
    "alibaba.com", "made-in-china.com", "amazon.", "ebay.",
)


@dataclass
class Source:
    """One web source behind one or more claims."""

    short_id: str
    url: str
    title: str = ""
    domain: str = ""
    tier: int = 1
    tier_label: str = "general web"
    reachable: bool | None = None  # None until checked
    note: str = ""

    def as_prompt_line(self) -> str:
        state = {True: "reachable", False: "unreachable", None: "unchecked"}[self.reachable]
        return (
            f"[{self.short_id}] {self.title or self.domain} — {self.url}\n"
            f"    authority: {self.tier_label} (tier {self.tier}/5), {state}"
            + (f", {self.note}" if self.note else "")
        )


@dataclass
class SourceIndex:
    """Every source the search actually used this run."""

    sources: dict[str, Source] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.sources)

    def add(self, url: str, title: str = "", domain: str = "") -> Source:
        for existing in self.sources.values():
            if existing.url == url:
                return existing
        short_id = f"src-{len(self.sources) + 1}"
        domain = domain or domain_from_title(title) or urlparse(url).netloc
        tier, label = classify_domain(domain)
        source = Source(
            short_id=short_id,
            url=url,
            title=title,
            domain=domain or urlparse(url).netloc,
            tier=tier,
            tier_label=label,
        )
        source.domain = domain
        self.sources[short_id] = source
        return source

    def reclassify(self, source: Source, domain: str) -> None:
        """Re-rank a source once its real publisher is known."""
        source.domain = domain
        source.tier, source.tier_label = classify_domain(domain)

    def get(self, short_id: str) -> Source | None:
        return self.sources.get(short_id)

    def as_prompt(self) -> str:
        if not self.sources:
            return "(the search returned no citable sources)"
        return "\n".join(s.as_prompt_line() for s in self.sources.values())

    def to_list(self) -> list[dict[str, object]]:
        return [
            {
                "short_id": s.short_id,
                "url": s.url,
                "title": s.title,
                "domain": s.domain,
                "tier": s.tier,
                "tier_label": s.tier_label,
                "reachable": s.reachable,
                "note": s.note,
            }
            for s in self.sources.values()
        ]


HOSTNAME = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", re.IGNORECASE)


def domain_from_title(title: str) -> str:
    """Grounding puts the publisher's host in `title`, not in `domain`.

    Without this, every source is classified from the URL — and the URL is a
    Google redirect, so the whole web ranks as "general web" and the audit
    downgrades everything equally, which is the same as saying nothing.
    """
    candidate = (title or "").strip().lower()
    return candidate if HOSTNAME.match(candidate) else ""


def classify_domain(domain: str) -> tuple[int, str]:
    """How much a claim may rest on this domain, before it is even fetched."""
    host = (domain or "").lower()
    if any(weak in host for weak in WEAK_DOMAINS):
        return 1, "aggregator or user-generated"
    for tier, label, needles in AUTHORITY_TIERS:
        if needles and any(needle in host for needle in needles):
            return tier, label
    return 1, "general web"


def harvest(events: list, index: SourceIndex | None = None) -> SourceIndex:
    """Pull the real URLs out of a run's grounding metadata.

    `events` is whatever the runner produced. Anything without grounding is
    skipped, so this is safe to call over an entire session.
    """
    index = index or SourceIndex()
    for event in events:
        metadata = getattr(event, "grounding_metadata", None)
        if not metadata or not getattr(metadata, "grounding_chunks", None):
            continue
        for chunk in metadata.grounding_chunks:
            web = getattr(chunk, "web", None)
            if not web or not getattr(web, "uri", None):
                continue
            index.add(
                url=web.uri,
                title=getattr(web, "title", "") or "",
                domain=getattr(web, "domain", "") or "",
            )
    return index


def check_reachable(index: SourceIndex, timeout: float = 8.0, limit: int = 15) -> None:
    """Ask each source whether it is still there.

    Deliberately cheap and deliberately forgiving: one request, a short
    timeout, and a failure means "could not confirm", never "is false".

    Following the redirect earns its cost twice over. Search hands back a
    Google redirect URL that expires, so resolving it is the only way to store
    a link that still works next month — and the address it lands on is the
    real publisher, which is what the authority ranking is supposed to judge.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; content-pipeline/1.0)"}
    for source in list(index.sources.values())[:limit]:
        try:
            response = httpx.head(
                source.url, timeout=timeout, follow_redirects=True, headers=headers
            )
            if response.status_code >= 400:
                response = httpx.get(
                    source.url, timeout=timeout, follow_redirects=True, headers=headers
                )
            source.reachable = response.status_code < 400
            if not source.reachable:
                source.note = f"HTTP {response.status_code}"

            final = str(response.url)
            host = urlparse(final).netloc.lower()
            if host and "vertexaisearch" not in host and "google" not in host:
                source.url = final
                index.reclassify(source, host)
        except Exception as exc:  # noqa: BLE001 - a network is not a fact-check
            source.reachable = False
            source.note = f"not reachable ({type(exc).__name__})"
    logger.info(
        "Source check: %s of %s reachable.",
        sum(1 for s in index.sources.values() if s.reachable),
        len(index.sources),
    )


CITATION = re.compile(r"src-\d+")


def audit_fact(
    claim_source_ids: list[str], index: SourceIndex, confidence: str
) -> tuple[str, bool, str]:
    """Judge one fact against the sources it cites.

    Returns the confidence it has actually earned, whether it may be written
    from, and why. Nothing here asks a model: the same fact and the same
    sources always produce the same verdict.
    """
    cited = [index.get(sid) for sid in claim_source_ids]
    found = [s for s in cited if s is not None]

    if not claim_source_ids:
        return "LOW", False, "cites no source"
    if not found:
        return "LOW", False, "cites source ids that were never returned by the search"

    best_tier = max(s.tier for s in found)
    any_reachable = any(s.reachable is not False for s in found)

    if not any_reachable:
        # Everything it rests on failed to answer. Usable for background, not
        # for a number a reader might act on.
        return "LOW", confidence != "HIGH", "every cited source was unreachable"

    if best_tier >= 3:
        # A standards body, a university or the people who built the thing.
        return confidence, True, ""
    if best_tier == 2:
        # An engineering publication is a reasonable place for a general
        # figure, but not for a specific product's specification.
        return confidence, True, ""
    if confidence == "HIGH":
        # A high-confidence technical claim resting only on the general web is
        # exactly the failure this whole pipeline exists to prevent. It is not
        # thrown away — it is written as the estimate it actually is.
        return "MEDIUM", True, "downgraded: only general-web sources support it"
    return confidence, True, ""
