"""Where a claim actually came from.

Search grounding does not put its URLs in the model's text — it puts them in
the response's grounding metadata. Without harvesting that, every source in
the registry is whatever the model chose to type, and the chain of evidence
ends inside the model that made the claim.

This module harvests the real URLs and then asks three questions of them, in
increasing order of cost:

  Is it there?        one cheap request, following the redirect to the real
                      publisher, which is also what the ranking judges.
  Who published it?   the people who built the thing outrank the people
                      reselling it, and both outrank someone's blog.
  Does it say that?   the page a fact cites is read, and the passage the fact
                      quotes is looked for in it. This is the check that
                      closes the loop: until it existed, a model could quote a
                      real page as saying something it never said.

Every failure here lowers a claim's confidence rather than killing the run.
The pipeline runs from networks where half the internet is a coin flip, and a
run that fails on a timeout publishes nothing at all.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .config import SourceConfig
from .normalize import normalize_text, numbers_in, shingles, tokens

logger = logging.getLogger(__name__)

# Authorities that mean the same thing whatever a site writes about. Anything
# subject-specific — which manufacturers, which trade publications — is
# configuration, because a list of solar companies compiled in here would make
# this pipeline quietly wrong for every site that is not about solar.
STANDARDS_BODIES = (".gov", ".gov.", ".int", "iso.org", "iec.ch", "ieee.org", "itu.int")
ACADEMIC = (
    ".edu", ".ac.", "researchgate.net", "sciencedirect.com", "nature.com",
    "springer.com", "mdpi.com", "arxiv.org", "doi.org",
)
# Aggregators, forums and content farms: they may be right, but nothing
# technical should rest on them alone.
WEAK_DOMAINS = (
    "wikipedia.org", "quora.com", "reddit.com", "medium.com", "linkedin.com",
    "facebook.com", "pinterest.com", "youtube.com", "tiktok.com", "blogspot.",
    "wordpress.com", "alibaba.com", "made-in-china.com", "amazon.", "ebay.",
)

# How much of a quoted passage must survive on the page for the quote to count
# as a quote rather than a resemblance — measured in word runs for the first,
# and in plain words for the second, where the sentence was rebuilt rather than
# copied.
QUOTED = 0.85
PARAPHRASED = 0.6
# Less text than this is a paywall, a cookie wall or a page that builds itself
# in the browser — none of which is evidence that a passage is absent.
MIN_PAGE_TEXT = 400
MAX_FETCH_BYTES = 1_000_000
TEXT_TYPES = ("text/html", "text/plain", "application/xhtml", "application/xml")


@dataclass(frozen=True)
class Authority:
    """The ranking, assembled from what is universal plus what a site configures."""

    tiers: tuple[tuple[int, str, tuple[str, ...]], ...]
    weak: tuple[str, ...]


def authority_from(config: SourceConfig | None = None) -> Authority:
    config = config or SourceConfig()
    return Authority(
        tiers=(
            (5, "standards body or government", STANDARDS_BODIES + config.standards),
            (4, "academic or research", ACADEMIC + config.academic),
            (3, "manufacturer documentation", config.manufacturers),
            (2, "industry or engineering publication", config.publications),
        ),
        weak=WEAK_DOMAINS + config.weak_domains,
    )


DEFAULT_AUTHORITY = authority_from()


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
    published: str = ""
    age_days: int | None = None
    # Page text, held only for the length of the audit. It is never written to
    # state or to the database: it is large, and it is worth nothing once the
    # question it answers has been answered.
    text: str = ""
    # A source carried over from the registry, whose passage was checked when
    # the fact was first verified and whose shelf life has not run out.
    from_registry: bool = False
    # Cached by page_analysis(); never serialised.
    _analysis: tuple[str, set[str], set[str], set[str]] | None = field(
        default=None, repr=False, compare=False
    )

    def as_prompt_line(self) -> str:
        state = {True: "reachable", False: "unreachable", None: "unchecked"}[self.reachable]
        age = f", published {self.published}" if self.published else ""
        return (
            f"[{self.short_id}] {self.title or self.domain} — {self.url}\n"
            f"    authority: {self.tier_label} (tier {self.tier}/5), {state}{age}"
            + (f", {self.note}" if self.note else "")
        )

    @property
    def readable(self) -> bool:
        """Whether enough of the page came back to say anything about a quote."""
        return len(self.text) >= MIN_PAGE_TEXT

    def older_than(self, days: int) -> bool:
        return bool(days) and self.age_days is not None and self.age_days > days

    def page_analysis(self) -> tuple[str, set[str], set[str], set[str]]:
        """The page as the passage check reads it, computed once.

        Normalised text, its word set, its word runs and its numbers. Cached
        because every fact citing this source asks the same four questions of
        the same megabyte of text.
        """
        if self._analysis is None:
            norm = normalize_text(self.text)
            words = norm.split()
            self._analysis = (norm, set(words), shingles(words), numbers_in(self.text))
        return self._analysis


@dataclass
class SourceIndex:
    """Every source this run may cite: what the search found, plus what the
    registry already verified."""

    sources: dict[str, Source] = field(default_factory=dict)
    authority: Authority = DEFAULT_AUTHORITY

    def __bool__(self) -> bool:
        return bool(self.sources)

    def add(self, url: str, title: str = "", domain: str = "") -> Source:
        for existing in self.sources.values():
            if existing.url == url:
                return existing
        found = sum(1 for s in self.sources if s.startswith("src-"))
        domain = domain or domain_from_title(title) or urlparse(url).netloc
        tier, label = classify_domain(domain, self.authority)
        source = Source(
            short_id=f"src-{found + 1}",
            url=url,
            title=title,
            domain=domain or urlparse(url).netloc,
            tier=tier,
            tier_label=label,
        )
        self.sources[source.short_id] = source
        return source

    def add_known(
        self,
        short_id: str,
        url: str,
        *,
        title: str = "",
        tier: int = 1,
        tier_label: str = "general web",
        verified_at: str = "",
    ) -> Source:
        """Put a source the registry already verified back on the table.

        It keeps the authority it earned when it was first checked, so a fact
        reused from the registry audits exactly as it did the day it was
        stored — and it is exempt from the passage check, because that is what
        the shelf life is for. Re-reading the same page every morning would
        cost a request per fact to learn what expiry already knows.
        """
        source = Source(
            short_id=short_id,
            url=url,
            title=title,
            domain=urlparse(url).netloc,
            tier=tier,
            tier_label=tier_label,
            note=f"verified {verified_at}" if verified_at else "previously verified",
            from_registry=True,
        )
        self.sources[short_id] = source
        return source

    def reclassify(self, source: Source, domain: str) -> None:
        """Re-rank a source once its real publisher is known."""
        source.domain = domain
        source.tier, source.tier_label = classify_domain(domain, self.authority)

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
                "published": s.published,
                "age_days": s.age_days,
                "from_registry": s.from_registry,
            }
            for s in self.sources.values()
        ]

    @classmethod
    def from_list(
        cls, entries: list[dict], authority: Authority = DEFAULT_AUTHORITY
    ) -> SourceIndex:
        """Rebuild the index a previous step stored in state."""
        index = cls(authority=authority)
        for entry in entries or []:
            source = Source(
                short_id=str(entry.get("short_id") or ""),
                url=str(entry.get("url") or ""),
                title=str(entry.get("title") or ""),
                domain=str(entry.get("domain") or ""),
                tier=int(entry.get("tier") or 1),
                tier_label=str(entry.get("tier_label") or "general web"),
                note=str(entry.get("note") or ""),
                published=str(entry.get("published") or ""),
                from_registry=bool(entry.get("from_registry")),
            )
            source.reachable = entry.get("reachable")
            age = entry.get("age_days")
            source.age_days = int(age) if isinstance(age, int) else None
            if source.short_id:
                index.sources[source.short_id] = source
        return index


HOSTNAME = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", re.IGNORECASE)


def domain_from_title(title: str) -> str:
    """Grounding puts the publisher's host in `title`, not in `domain`.

    Without this, every source is classified from the URL — and the URL is a
    Google redirect, so the whole web ranks as "general web" and the audit
    downgrades everything equally, which is the same as saying nothing.
    """
    candidate = (title or "").strip().lower()
    return candidate if HOSTNAME.match(candidate) else ""


def classify_domain(
    domain: str, authority: Authority = DEFAULT_AUTHORITY
) -> tuple[int, str]:
    """How much a claim may rest on this domain, before it is even fetched."""
    host = (domain or "").lower()
    if any(weak in host for weak in authority.weak):
        return 1, "aggregator or user-generated"
    for tier, label, needles in authority.tiers:
        if needles and any(needle in host for needle in needles):
            return tier, label
    return 1, "general web"


def harvest(events: list, index: SourceIndex | None = None) -> SourceIndex:
    """Pull the real URLs out of a run's grounding metadata.

    `events` is whatever the runner produced. Anything without grounding is
    skipped, so this is safe to call over an entire session.
    """
    # `is None` rather than `or`: an empty index is falsy, and an index carrying
    # a site's configured authority is empty exactly when it is handed over.
    index = SourceIndex() if index is None else index
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


USER_AGENT = {"User-Agent": "Mozilla/5.0 (compatible; content-pipeline/1.0)"}


def check_reachable(index: SourceIndex, timeout: float = 8.0, limit: int = 15) -> None:
    """Ask each source whether it is still there.

    Deliberately cheap and deliberately forgiving: one request, a short
    timeout, and a failure means "could not confirm", never "is false".

    Following the redirect earns its cost twice over. Search hands back a
    Google redirect URL that expires, so resolving it is the only way to store
    a link that still works next month — and the address it lands on is the
    real publisher, which is what the authority ranking is supposed to judge.
    """
    for source in list(index.sources.values())[:limit]:
        if source.from_registry:
            continue
        try:
            response = httpx.head(
                source.url, timeout=timeout, follow_redirects=True, headers=USER_AGENT
            )
            if response.status_code >= 400:
                response = httpx.get(
                    source.url, timeout=timeout, follow_redirects=True, headers=USER_AGENT
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


# ------------------------------------------------------------- reading a page

SCRIPTS = re.compile(r"<(script|style|noscript|template)\b.*?</\1>", re.S | re.I)
TAGS = re.compile(r"<[^>]+>")
META_DATE = re.compile(
    r"<meta[^>]+(?:property|name|itemprop)=[\"'](?:article:published_time|"
    r"datePublished|publish(?:ed)?[-_]?date|date|DC.date.issued)[\"'][^>]*"
    r"content=[\"']([^\"']+)[\"']",
    re.I,
)
META_DATE_REVERSED = re.compile(
    r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name|itemprop)="
    r"[\"'](?:article:published_time|datePublished|date)[\"']",
    re.I,
)
JSON_LD_DATE = re.compile(r"[\"']datePublished[\"']\s*:\s*[\"']([^\"']+)[\"']", re.I)
TIME_TAG = re.compile(r"<time[^>]+datetime=[\"']([^\"']+)[\"']", re.I)


def page_text(raw: str) -> str:
    """The words a reader would see, out of the markup around them."""
    without_code = SCRIPTS.sub(" ", raw)
    return re.sub(r"\s+", " ", html_module.unescape(TAGS.sub(" ", without_code))).strip()


def published_date(raw: str) -> str:
    """When the page says it was published, if it says at all.

    Four conventions, because publishers use all of them and a source with no
    date is treated as undated rather than as fresh — the opposite mistake
    would quietly bless everything old.
    """
    for pattern in (META_DATE, META_DATE_REVERSED, JSON_LD_DATE, TIME_TAG):
        match = pattern.search(raw)
        if match:
            stamp = match.group(1).strip()[:32]
            parsed = _parse_date(stamp)
            if parsed:
                return parsed.date().isoformat()
    return ""


def _parse_date(stamp: str) -> datetime | None:
    for candidate in (stamp, stamp[:10]):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def age_in_days(published: str) -> int | None:
    parsed = _parse_date(published)
    if not parsed:
        return None
    return max((datetime.now(timezone.utc) - parsed).days, 0)


def fetch_pages(
    index: SourceIndex,
    short_ids: list[str],
    config: SourceConfig | None = None,
    timeout: float = 12.0,
) -> None:
    """Read the pages some fact actually cites.

    Only those: the whole point of doing this after the registry is built is
    that a search returns twenty sources and the article rests on six. A page
    that is not HTML — a datasheet PDF, most often, which is the best source
    there is — cannot be read here, and is left unchecked rather than counted
    as missing the passage.
    """
    config = config or SourceConfig()
    if not config.verify_evidence:
        return

    wanted = [
        index.sources[sid]
        for sid in dict.fromkeys(short_ids)
        if sid in index.sources
        and not index.sources[sid].from_registry
        and index.sources[sid].reachable is not False
    ][: config.fetch_limit]

    for source in wanted:
        try:
            with httpx.stream(
                "GET",
                source.url,
                timeout=timeout,
                follow_redirects=True,
                headers=USER_AGENT,
            ) as response:
                if response.status_code >= 400:
                    source.reachable = False
                    source.note = source.note or f"HTTP {response.status_code}"
                    continue
                content_type = response.headers.get("content-type", "").lower()
                if not any(kind in content_type for kind in TEXT_TYPES):
                    source.note = source.note or "not a readable page"
                    continue
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_FETCH_BYTES:
                        break
                raw = b"".join(chunks).decode(
                    response.encoding or "utf-8", errors="replace"
                )
        except Exception as exc:  # noqa: BLE001 - a network is not a fact-check
            source.note = source.note or f"could not be read ({type(exc).__name__})"
            continue

        source.text = page_text(raw)
        source.published = published_date(raw)
        source.age_days = age_in_days(source.published)

    logger.info(
        "Read %s of %s cited page(s) to look for the passages they were quoted for.",
        sum(1 for s in wanted if s.readable),
        len(wanted),
    )


def passage_state(evidence: str, source: Source) -> str:
    """Whether this page says what it was cited as saying.

    Three answers, and the third is the one that matters: `absent` means the
    page was read, had real content, and did not contain the passage — which
    is a claim resting on a source that does not support it, however real the
    source is.
    """
    if source.from_registry:
        return "quoted"
    if not evidence.strip() or not source.readable:
        return "unchecked"

    # The page's side of the comparison is computed once per source, however
    # many facts cite it: normalising and shingling a megabyte of text is real
    # CPU, and twenty facts on the same datasheet would repeat it twenty times.
    norm_page, page_words, page_shingles, page_numbers = source.page_analysis()

    words = tokens(evidence)
    if normalize_text(evidence) in norm_page:
        return "quoted"
    wanted_shingles = shingles(words)
    if wanted_shingles and (
        len(wanted_shingles & page_shingles) / len(wanted_shingles) >= QUOTED
    ):
        return "quoted"

    # Numbers decide the close calls. Vocabulary repeats across a subject and
    # proves little by itself, but a page carrying every figure the passage
    # states as well as most of its words is the page the passage came from.
    wanted = numbers_in(evidence)
    figures_present = not wanted or wanted <= page_numbers
    coverage = len(set(words) & page_words) / len(set(words)) if words else 0.0
    if figures_present and coverage >= PARAPHRASED:
        return "paraphrased"
    return "absent"


CITATION = re.compile(r"src-\d+")

_STEP_DOWN = {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}


@dataclass(frozen=True)
class FactAudit:
    """What one fact earned from the sources it cites."""

    confidence: str
    allowed: bool
    note: str = ""
    verified: bool = False


def audit_fact(
    claim_source_ids: list[str],
    index: SourceIndex,
    confidence: str,
    evidence: str = "",
    config: SourceConfig | None = None,
) -> FactAudit:
    """Judge one fact against the sources it cites.

    Returns the confidence it has actually earned, whether it may be written
    from, and why. Nothing here asks a model: the same fact and the same
    sources always produce the same verdict.
    """
    cited = [index.get(sid) for sid in claim_source_ids]
    found = [s for s in cited if s is not None]

    if not claim_source_ids:
        return FactAudit("LOW", False, "cites no source")
    if not found:
        return FactAudit(
            "LOW", False, "cites source ids that were never returned by the search"
        )

    if all(s.reachable is False for s in found):
        # Everything it rests on failed to answer. Usable for background, not
        # for a number a reader might act on.
        return FactAudit(
            "LOW", confidence != "HIGH", "every cited source was unreachable"
        )

    live = [s for s in found if s.reachable is not False]
    states = [passage_state(evidence, s) for s in live]
    if "quoted" not in states and "paraphrased" not in states and "absent" in states:
        # The page was read and does not contain what it was quoted for. This
        # is the failure no reviewer can catch, because the article and the
        # registry agree with each other perfectly.
        return FactAudit(
            "LOW",
            confidence != "HIGH",
            "the quoted passage was not found on the page it cites",
        )

    verified = "quoted" in states or "paraphrased" in states
    notes: list[str] = []
    if "paraphrased" in states and "quoted" not in states:
        notes.append("the source supports this in substance rather than word for word")

    best = max(live, key=lambda s: s.tier)
    earned = confidence

    if best.tier <= 1 and confidence == "HIGH":
        # A high-confidence technical claim resting only on the general web is
        # exactly the failure this whole pipeline exists to prevent. It is not
        # thrown away — it is written as the estimate it actually is.
        earned = "MEDIUM"
        notes.append("downgraded: only general-web sources support it")

    # Age only counts against the sources where age means anything. A datasheet
    # from four years ago is still the datasheet; a market figure that old is a
    # guess dressed as a fact.
    max_age = (config or SourceConfig()).max_age_days
    if best.tier <= 2 and best.older_than(max_age):
        stepped = _STEP_DOWN[earned]
        if stepped != earned:
            earned = stepped
            notes.append(f"the best source is {best.age_days} days old")

    return FactAudit(earned, True, "; ".join(notes), verified)
