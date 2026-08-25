"""Measuring a draft against itself.

Most on-page SEO is not an opinion. A title is either short enough to survive a
search result or it is not; a heading level either follows the one above it or
skips; an internal link either resolves to a page that exists or sends the
reader nowhere. Asking a language model to score those is asking it to count,
which it does inconsistently and cannot be held to.

So this module measures, and nothing here decides. It returns findings; whether
a finding blocks a draft is settled in `rules.py`, beside the citation check,
because the pipeline has exactly one gate and it is written in Python.

The findings come back as `ReviewIssue` — the same shape the reviewers speak —
so the judge that turns findings into edits needs no special case for them.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import SeoConfig
from .images import IMAGE_MARKER
from .schemas import ArticleDraft, ReviewIssue, Severity

# A heading line, with the closing hashes some writers add stripped off.
HEADING_LINE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>\S.*?)\s*#*$")
FENCE_LINE = re.compile(r"^\s{0,3}(```|~~~)")
# A Markdown link, but not an image: the leading `!` is what tells them apart.
LINK = re.compile(r"(?<!!)\[(?P<text>[^\]]*)\]\((?P<href>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
# Anything that opens like an image marker, whether or not it is well formed.
MARKER_OPENING = re.compile(r"\[\[\s*IMAGE")
SLUG_FORMAT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Question marks in the scripts this pipeline is likely to write in. A heading
# that ends in one is a question the article answers, which is what makes the
# FAQ structured data honest rather than invented.
QUESTION_MARKS = ("?", "؟", "？")  # noqa: RUF001 - the last two are not the first

# Links that are not navigation and cannot be broken.
NON_NAVIGATION = ("mailto:", "tel:", "sms:", "javascript:")


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    line: int

    @property
    def is_question(self) -> bool:
        return self.text.rstrip().endswith(QUESTION_MARKS)


@dataclass(frozen=True)
class Link:
    text: str
    href: str
    line: int


def _body_lines(body: str) -> list[tuple[int, str]]:
    """Numbered lines with fenced code blocks removed.

    A `# comment` inside a shell example is not a heading, and a URL inside a
    code sample is not a link the reader can click.
    """
    kept: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(body.splitlines(), start=1):
        if FENCE_LINE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            kept.append((number, line))
    return kept


def headings(body: str) -> list[Heading]:
    """Every Markdown heading in the body, in order."""
    found: list[Heading] = []
    for number, line in _body_lines(body):
        match = HEADING_LINE.match(line)
        if match:
            found.append(
                Heading(
                    level=len(match.group("hashes")),
                    text=match.group("text").strip(),
                    line=number,
                )
            )
    return found


def links(body: str) -> list[Link]:
    """Every Markdown link in the body, images excluded."""
    return [
        Link(text=m.group("text").strip(), href=m.group("href").strip(), line=number)
        for number, line in _body_lines(body)
        for m in LINK.finditer(line)
    ]


def opening_text(body: str) -> str:
    """The first real paragraph — what a reader sees before deciding to stay."""
    for _, line in _body_lines(body):
        stripped = line.strip()
        if not stripped or HEADING_LINE.match(line) or MARKER_OPENING.search(stripped):
            continue
        return stripped
    return ""


def _script(text: str) -> str:
    """The writing system this text is mostly in.

    Used to decide whether a keyword can meaningfully be looked for in a title
    at all: an English keyword will never appear literally in a Persian
    sentence, and demanding it would either be ignored or answered by dropping
    a foreign word into the prose — which is the exact defect the writer is
    told to avoid.
    """
    counts: Counter[str] = Counter()
    for char in text:
        if not char.isalpha():
            continue
        code = ord(char)
        if code < 0x0250:
            counts["latin"] += 1
        elif 0x0400 <= code <= 0x04FF:
            counts["cyrillic"] += 1
        elif 0x0590 <= code <= 0x05FF:
            counts["hebrew"] += 1
        elif 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F:
            counts["arabic"] += 1
        elif 0xFB50 <= code <= 0xFDFF or 0xFE70 <= code <= 0xFEFF:
            counts["arabic"] += 1
        elif 0x0900 <= code <= 0x097F:
            counts["devanagari"] += 1
        elif 0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF:
            counts["cjk"] += 1
        else:
            counts["other"] += 1
    return counts.most_common(1)[0][0] if counts else ""


def path_of(href: str) -> str:
    """The path part of a link, without query, fragment or trailing slash."""
    return urlsplit(href).path.rstrip("/")


def slug_of(href: str) -> str:
    """The last path segment — how a page is named on nearly every site."""
    path = path_of(href)
    return path.rsplit("/", 1)[-1].strip().lower() if path else ""


def slugs_from(entries: list[dict]) -> list[str]:
    """Pull the addressable name out of whatever shape the site returned.

    Sites disagree about what the field is called, and some send a full URL
    where others send a bare slug. Reading several keys here is cheaper than
    demanding one shape from every site this pipeline might serve.
    """
    found: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        for key in ("slug", "path", "url", "permalink"):
            value = str(entry.get(key) or "").strip()
            if value:
                found.append(value)
                break
    return found


@dataclass(frozen=True)
class SiteIndex:
    """The pages that actually exist, so a link can be checked rather than trusted.

    Everything here was fetched from the site at the start of the run. A link
    the writer invents is the failure this catches, and it is worth catching in
    code: a reader who clicks into a 404 learns not to click again, and a search
    engine reads the same signal.
    """

    slugs: frozenset[str] = frozenset()
    paths: frozenset[str] = frozenset()
    public_host: str = ""

    @classmethod
    def from_slugs(
        cls,
        slugs: list[str] | tuple[str, ...] = (),
        extra_paths: tuple[str, ...] = (),
        public_url: str = "",
    ) -> SiteIndex:
        return cls(
            slugs=frozenset(s for s in (slug_of(x) or x.lower() for x in slugs) if s),
            paths=frozenset(
                p.rstrip("/").lower() for p in extra_paths if p.strip()
            ),
            public_host=urlsplit(public_url).netloc.lower() if public_url else "",
        )

    def is_internal(self, href: str) -> bool:
        """Whether this link points back at the site we are writing for."""
        candidate = href.strip()
        if not candidate or candidate.startswith("#"):
            return False
        if candidate.lower().startswith(NON_NAVIGATION):
            return False
        split = urlsplit(candidate)
        if split.scheme or split.netloc:
            return bool(self.public_host) and split.netloc.lower() == self.public_host
        return True

    def knows(self, href: str) -> bool:
        path = path_of(href).lower()
        if path in self.paths:
            return True
        # The site's own front page is always reachable, whatever it is called.
        if path in ("", "/"):
            return True
        return slug_of(href) in self.slugs

    @property
    def is_empty(self) -> bool:
        """True when the site told us nothing — checks then have no ground truth."""
        return not self.slugs and not self.paths


def _issue(
    issue_id: str, severity: Severity, location: str, problem: str, fix: str
) -> ReviewIssue:
    return ReviewIssue(
        issue_id=issue_id,
        severity=severity,
        location=location,
        problem=problem,
        required_fix=fix,
    )


def _listing_defects(draft: ArticleDraft, config: SeoConfig) -> list[ReviewIssue]:
    """The search result itself: the one thing every searcher sees."""
    found: list[ReviewIssue] = []
    title = draft.seo_title.strip()
    if not title:
        found.append(
            _issue(
                "SEO-TITLE-MISSING",
                "major",
                "seo_title",
                "seo_title is empty, so the search result falls back to the article title.",
                f"Write an seo_title of up to {config.title_max} characters that leads "
                "with the words a searcher would type.",
            )
        )
    else:
        if len(title) > config.title_max:
            found.append(
                _issue(
                    "SEO-TITLE-LONG",
                    "major",
                    "seo_title",
                    f"seo_title is {len(title)} characters; it is cut off after about "
                    f"{config.title_max}.",
                    f"Shorten seo_title to {config.title_max} characters or fewer, "
                    "keeping the searched words at the front.",
                )
            )
        elif len(title) < config.title_min:
            found.append(
                _issue(
                    "SEO-TITLE-SHORT",
                    "minor",
                    "seo_title",
                    f"seo_title is only {len(title)} characters and wastes room a "
                    "searcher would have read.",
                    f"Extend seo_title toward {config.title_max} characters with what "
                    "the reader gets.",
                )
            )
        if title.strip() == draft.title.strip():
            found.append(
                _issue(
                    "SEO-TITLE-COPY",
                    "minor",
                    "seo_title",
                    "seo_title repeats the article title verbatim, so the search "
                    "listing adds nothing the page does not already say.",
                    "Rewrite seo_title as the question a searcher types, answered.",
                )
            )

    description = draft.meta_description.strip()
    if not description:
        found.append(
            _issue(
                "SEO-META-MISSING",
                "major",
                "meta_description",
                "meta_description is empty, so the search snippet is whatever the "
                "engine scrapes off the page.",
                f"Write about {config.description_min}-{config.description_max} "
                "characters saying what the reader gets and why to open it.",
            )
        )
    else:
        if len(description) > config.description_max:
            found.append(
                _issue(
                    "SEO-META-LONG",
                    "major",
                    "meta_description",
                    f"meta_description is {len(description)} characters and is cut "
                    f"off after about {config.description_max}.",
                    f"Cut meta_description to {config.description_max} characters "
                    "or fewer without losing the reason to click.",
                )
            )
        elif len(description) < config.description_min:
            found.append(
                _issue(
                    "SEO-META-SHORT",
                    "minor",
                    "meta_description",
                    f"meta_description is only {len(description)} characters and "
                    "leaves the snippet half empty.",
                    f"Extend meta_description toward {config.description_max} "
                    "characters.",
                )
            )
        if description.strip() == draft.excerpt.strip():
            found.append(
                _issue(
                    "SEO-META-COPY",
                    "minor",
                    "meta_description",
                    "meta_description is a copy of the excerpt, so the same sentence "
                    "greets the reader twice.",
                    "Write meta_description for the search result specifically.",
                )
            )
    return found


def _structure_defects(draft: ArticleDraft) -> list[ReviewIssue]:
    """Headings, and the one H1 rule.

    The pipeline sends the title as its own field and the site renders it as the
    page heading, so an H1 inside the body is a second one competing with it.
    Body headings therefore start at level two.
    """
    found: list[ReviewIssue] = []
    structure = headings(draft.body)

    if not structure:
        found.append(
            _issue(
                "SEO-NO-HEADINGS",
                "major",
                "body",
                "The body has no headings at all; the reader gets an unbroken wall "
                "of text and no way to find the part they came for.",
                "Break the body into sections with `##` headings from the outline.",
            )
        )
        return found

    for heading in (h for h in structure if h.level == 1):
        found.append(
            _issue(
                "SEO-H1-IN-BODY",
                "major",
                f"body line {heading.line}",
                f"`# {heading.text}` is an H1 inside the body, but the site already "
                "renders the article title as the page's H1.",
                "Demote it to `##` so the page has exactly one top-level heading.",
            )
        )

    previous = 1
    for heading in structure:
        if heading.level > previous + 1:
            found.append(
                _issue(
                    f"SEO-HEADING-JUMP-{heading.line}",
                    "minor",
                    f"body line {heading.line}",
                    f"The heading level jumps from H{previous} to H{heading.level}, "
                    "which breaks the outline a screen reader and a crawler both read.",
                    f"Use H{previous + 1} here, or add the level that is missing above it.",
                )
            )
        previous = heading.level
    return found


def _image_defects(draft: ArticleDraft, config: SeoConfig) -> list[ReviewIssue]:
    """Alt text, and markers that would be published as literal text."""
    found: list[ReviewIssue] = []

    opened = len(MARKER_OPENING.findall(draft.body))
    well_formed = list(IMAGE_MARKER.finditer(draft.body))
    if opened > len(well_formed):
        found.append(
            _issue(
                "SEO-IMAGE-MARKER",
                "major",
                "body",
                f"{opened - len(well_formed)} image marker(s) are malformed and would "
                "be published as literal `[[IMAGE: ...]]` text.",
                "Write every marker as `[[IMAGE: what it shows | alt text]]`, on its "
                "own line.",
            )
        )

    for index, match in enumerate(well_formed, start=1):
        alt = match.group("alt").strip()
        if len(alt) < config.alt_min:
            found.append(
                _issue(
                    f"SEO-IMAGE-ALT-{index}",
                    "minor",
                    f"image marker {index}",
                    f"The alt text {alt!r} is too thin to describe the picture to "
                    "someone who cannot see it.",
                    "Describe what the picture actually shows, in the article's "
                    "language.",
                )
            )

    if draft.featured_image_prompt.strip() and not draft.featured_image_alt.strip():
        found.append(
            _issue(
                "SEO-LEAD-ALT",
                "major",
                "featured_image_alt",
                "A lead image was asked for with no alt text, so the largest picture "
                "on the page is invisible to a reader who cannot see it.",
                "Write featured_image_alt describing the lead image.",
            )
        )
    return found


def _slug_defects(draft: ArticleDraft, index: SiteIndex) -> list[ReviewIssue]:
    found: list[ReviewIssue] = []
    slug = draft.slug.strip()
    if not SLUG_FORMAT.match(slug):
        found.append(
            _issue(
                "SEO-SLUG-FORMAT",
                "major",
                "slug",
                f"{slug!r} is not a clean URL slug; it must be lowercase ASCII words "
                "joined by single hyphens.",
                "Rewrite the slug as lowercase ASCII with hyphens, even though the "
                "title is not in English.",
            )
        )
    elif slug in index.slugs:
        found.append(
            _issue(
                "SEO-SLUG-TAKEN",
                "critical",
                "slug",
                f"The slug {slug!r} already belongs to a page on the site. Publishing "
                "would either collide with it or quietly compete with it in search.",
                "Choose a slug that names what is specific about this article.",
            )
        )
    return found


def _link_defects(draft: ArticleDraft, index: SiteIndex) -> list[ReviewIssue]:
    """Internal links that go nowhere.

    Only checked when the site actually told us what it has published: with an
    empty index every link would look broken, and a gate that blocks every
    article because a fetch failed is worse than one that skips a check.
    """
    if index.is_empty:
        return []

    found: list[ReviewIssue] = []
    for number, link in enumerate(links(draft.body), start=1):
        if not index.is_internal(link.href) or index.knows(link.href):
            continue
        found.append(
            _issue(
                f"SEO-LINK-BROKEN-{number}",
                "critical",
                f"body line {link.line}",
                f"The internal link {link.href!r} does not match any page the site "
                "reported. A link into a 404 is the worst signal an article can send.",
                "Link to a published page from the list the site provided, or drop "
                "the link and keep the sentence.",
            )
        )
    return found


def _keyword_defects(
    draft: ArticleDraft, target_keywords: list[str]
) -> list[ReviewIssue]:
    """Whether the subject is stated where it counts.

    Only keywords written in the same script as the title are checked. An
    English keyword cannot appear literally in a Persian sentence, and a gate
    that demanded it would be asking the writer to commit the defect the
    reviewers are told to flag.
    """
    title = f"{draft.seo_title} {draft.title}".strip()
    if not title or not target_keywords:
        return []

    script = _script(title)
    checkable = [
        k.strip() for k in target_keywords if k.strip() and _script(k) == script
    ]
    if not checkable:
        return []

    found: list[ReviewIssue] = []
    haystack = title.casefold()
    if not any(k.casefold() in haystack for k in checkable):
        found.append(
            _issue(
                "SEO-KEYWORD-TITLE",
                "major",
                "title / seo_title",
                "Neither the title nor seo_title contains any target keyword: "
                + ", ".join(checkable[:5]),
                "Work the closest keyword into the title naturally, or say in the "
                "summary why a different phrasing serves the reader better.",
            )
        )

    opening = opening_text(draft.body).casefold()
    if opening and not any(k.casefold() in opening for k in checkable):
        found.append(
            _issue(
                "SEO-KEYWORD-OPENING",
                "minor",
                "the opening paragraph",
                "The opening paragraph never names the subject the article is "
                "meant to answer for: " + ", ".join(checkable[:5]),
                "Say plainly in the first paragraph what this article is about.",
            )
        )
    return found


def defects(
    draft: ArticleDraft,
    index: SiteIndex,
    target_keywords: list[str],
    config: SeoConfig,
) -> list[ReviewIssue]:
    """Everything measurable that is wrong with this draft.

    Ordered by where a reader meets it: the search listing, then the page, then
    the links out of it. Severity is assigned here but enforced in `rules.py`.
    """
    return [
        *_listing_defects(draft, config),
        *_structure_defects(draft),
        *_image_defects(draft, config),
        *_slug_defects(draft, index),
        *_link_defects(draft, index),
        *_keyword_defects(draft, target_keywords),
    ]
