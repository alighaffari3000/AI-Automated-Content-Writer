"""What the article tells a search engine about itself, in code.

Rich results are the one place where a wrong number is repeated by someone
else's system, in someone else's design, as a fact. So none of this is written
by a model: every value here is copied from something that was already checked
— the draft the gate approved, the catalogue the site handed us, the headings
the writer actually wrote.

Three shapes are emitted, and only when they are earned:

  Article   always; it is what the page is.
  FAQPage   when the article genuinely answers several questions, which is
            measured by counting question headings rather than asked.
  Product   when the draft names catalogue products, filled from the
            catalogue's own fields and never from the article's prose.

Dates are deliberately absent. The article leaves here as a draft and a human
decides when — or whether — it is published, so the only honest publication
date is the one the site stamps on it.
"""

from __future__ import annotations

import re

from .config import ContentConfig, SeoConfig, SiteConfig
from .images import IMAGE_MARKER
from .schemas import ArticleDraft
from .seo import HEADING_LINE, headings

SCHEMA_CONTEXT = "https://schema.org"

# schema.org treats a headline over about 110 characters as spam-ish, and
# Google truncates it anyway.
HEADLINE_MAX = 110
ANSWER_MAX = 1000

_MARKDOWN_LINK = re.compile(r"!?\[(?P<text>[^\]]*)\]\([^)]*\)")
_EMPHASIS = re.compile(r"[*_`]{1,3}")


def _clean(text: str) -> str:
    """Markdown prose as a reader would hear it read aloud."""
    without_images = IMAGE_MARKER.sub("", text)
    without_links = _MARKDOWN_LINK.sub(lambda m: m.group("text"), without_images)
    return re.sub(r"\s+", " ", _EMPHASIS.sub("", without_links)).strip()


def _url_for(slug: str, site: SiteConfig, prefix: str = "") -> str:
    if not site.public_url or not slug:
        return ""
    parts = [site.public_url.rstrip("/"), prefix.strip("/"), slug.strip("/")]
    return "/".join(part for part in parts if part)


def _organisation(site: SiteConfig) -> dict:
    entry: dict = {"@type": "Organization", "name": site.name}
    if site.public_url:
        entry["url"] = site.public_url.rstrip("/")
    return entry


def faq_pairs(body: str) -> list[tuple[str, str]]:
    """Question headings and the answer written under each one.

    A heading that ends in a question mark is the writer stating that this
    section answers a question — the only evidence available that does not
    require asking a model to summarise its own work.
    """
    lines = body.splitlines()
    structure = headings(body)
    pairs: list[tuple[str, str]] = []

    for position, heading in enumerate(structure):
        if not heading.is_question:
            continue
        following = structure[position + 1 :]
        end = following[0].line - 1 if following else len(lines)
        answer_lines = [
            line
            for line in lines[heading.line : end]
            if line.strip() and not HEADING_LINE.match(line)
        ]
        answer = _clean(" ".join(answer_lines))
        if answer:
            pairs.append((_clean(heading.text), answer[:ANSWER_MAX].strip()))
    return pairs


def article_entry(
    draft: ArticleDraft,
    *,
    site: SiteConfig,
    content: ContentConfig,
    keywords: list[str],
    featured_image: str = "",
) -> dict:
    headline = (draft.seo_title or draft.title).strip()[:HEADLINE_MAX]
    entry: dict = {
        "@context": SCHEMA_CONTEXT,
        "@type": "Article",
        "headline": headline,
        "description": (draft.meta_description or draft.excerpt).strip(),
        "inLanguage": content.language,
        "author": _organisation(site),
        "publisher": _organisation(site),
    }
    url = _url_for(draft.slug, site)
    if url:
        entry["mainEntityOfPage"] = {"@type": "WebPage", "@id": url}
    if featured_image:
        entry["image"] = [featured_image]
    if draft.category:
        entry["articleSection"] = draft.category
    if keywords:
        entry["keywords"] = list(keywords)
    return entry


def faq_entry(body: str, config: SeoConfig) -> dict | None:
    """An FAQPage, or nothing.

    Below the threshold there is no FAQ — there is an article with a question
    in a heading, and claiming otherwise to a search engine is the kind of
    small lie that costs a site its rich results entirely.
    """
    pairs = faq_pairs(body)
    if len(pairs) < config.min_faq_entries:
        return None
    return {
        "@context": SCHEMA_CONTEXT,
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": question,
                "acceptedAnswer": {"@type": "Answer", "text": answer},
            }
            for question, answer in pairs
        ],
    }


def _first(entry: dict, *keys: str) -> str:
    for key in keys:
        value = entry.get(key)
        if value not in (None, "", [], {}):
            return str(value).strip()
    return ""


def product_entries(
    draft: ArticleDraft, products: list[dict], site: SiteConfig
) -> list[dict]:
    """One entry per catalogue product the article actually discusses.

    Everything comes from the catalogue payload, so a specification the writer
    got wrong cannot reach a rich result through this path. Fields the site did
    not send are simply left out: an incomplete entry is fine, an invented one
    is not.
    """
    by_slug = {
        str(p.get("slug", "")).strip().lower(): p
        for p in products
        if isinstance(p, dict) and p.get("slug")
    }

    entries: list[dict] = []
    for slug in draft.related_products:
        product = by_slug.get(str(slug).strip().lower())
        if not product:
            continue

        entry: dict = {
            "@context": SCHEMA_CONTEXT,
            "@type": "Product",
            "name": _first(product, "name", "title") or str(slug),
        }
        for key, fields in (
            ("description", ("description", "excerpt", "summary")),
            ("sku", ("sku", "code", "model")),
            ("image", ("image", "image_url", "thumbnail")),
        ):
            value = _first(product, *fields)
            if value:
                entry[key] = value

        brand = _first(product, "brand", "manufacturer", "vendor")
        if brand:
            entry["brand"] = {"@type": "Brand", "name": brand}

        url = _first(product, "url", "permalink") or _url_for(
            str(slug), site, prefix="products"
        )
        if url:
            entry["url"] = url

        price = _first(product, "price", "amount")
        currency = _first(product, "currency", "currency_code")
        if price and currency:
            offer: dict = {
                "@type": "Offer",
                "price": price,
                "priceCurrency": currency,
            }
            availability = _first(product, "availability", "stock_status")
            if availability:
                offer["availability"] = availability
            entry["offers"] = offer

        entries.append(entry)
    return entries


def build(
    draft: ArticleDraft,
    *,
    body: str,
    site: SiteConfig,
    content: ContentConfig,
    config: SeoConfig,
    products: list[dict] | None = None,
    keywords: list[str] | None = None,
    featured_image: str = "",
) -> list[dict]:
    """Every JSON-LD block this article has earned, ready to embed."""
    if not config.structured_data:
        return []

    blocks = [
        article_entry(
            draft,
            site=site,
            content=content,
            keywords=keywords or [],
            featured_image=featured_image,
        )
    ]
    faq = faq_entry(body, config)
    if faq:
        blocks.append(faq)
    blocks.extend(product_entries(draft, products or [], site))
    return blocks
