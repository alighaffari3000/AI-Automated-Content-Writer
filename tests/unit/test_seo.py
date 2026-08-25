"""The measurements the gate counts on.

Nothing here calls a model. Each test states a draft that is wrong in exactly
one measurable way and asserts that the measurement finds it — and, just as
importantly, that a clean draft produces nothing at all, because a check that
fires on good work is a check the pipeline will learn to ignore.
"""

from __future__ import annotations

from app.config import SeoConfig
from app.schemas import ArticleDraft
from app.seo import (
    SiteIndex,
    defects,
    headings,
    links,
    opening_text,
    slugs_from,
)

CONFIG = SeoConfig(
    title_max=60,
    title_min=25,
    description_max=165,
    description_min=110,
    alt_min=10,
    known_paths=("/about",),
    min_faq_entries=2,
)

INDEX = SiteIndex.from_slugs(
    ["sizing-a-battery", "deye-sun-12k", "energy-storage"],
    extra_paths=CONFIG.known_paths,
    public_url="https://example.com",
)

GOOD_TITLE = "How to size a home battery without guessing"
GOOD_DESCRIPTION = (
    "Work out the battery capacity your house actually needs, from daily "
    "consumption and depth of discharge, with the arithmetic shown."
)
GOOD_BODY = """## What decides the size

Sizing a home battery comes down to two numbers: what you use overnight, and
how much of the battery you may actually take out.

## What that means when you buy

Take the overnight figure and divide it by usable depth of discharge.
"""


def draft(**overrides) -> ArticleDraft:
    """A draft with nothing measurably wrong with it."""
    fields = {
        "title": "Sizing a home battery",
        "slug": "sizing-a-home-battery",
        "excerpt": "A short guide to picking capacity from real consumption.",
        "body": GOOD_BODY,
        "seo_title": GOOD_TITLE,
        "meta_description": GOOD_DESCRIPTION,
    }
    fields.update(overrides)
    return ArticleDraft(**fields)


def ids(found) -> list[str]:
    return [issue.issue_id for issue in found]


def check(**overrides) -> list[str]:
    return ids(defects(draft(**overrides), INDEX, [], CONFIG))


def severity_of(issue_id: str, **overrides) -> str:
    found = defects(draft(**overrides), INDEX, [], CONFIG)
    return next(i.severity for i in found if i.issue_id.startswith(issue_id))


# ------------------------------------------------------------------ the parser


def test_a_clean_draft_produces_no_findings():
    assert check() == []


def test_headings_are_read_with_their_levels():
    found = headings("# One\n\ntext\n\n### Three\n")
    assert [(h.level, h.text) for h in found] == [(1, "One"), (3, "Three")]


def test_a_hash_inside_a_code_fence_is_not_a_heading():
    body = "## Real\n\n```bash\n# not a heading\n```\n\n## Also real\n"
    assert [h.text for h in headings(body)] == ["Real", "Also real"]


def test_images_are_not_counted_as_links():
    found = links("See [the guide](/sizing-a-battery) and ![a photo](/img.png)")
    assert [link.href for link in found] == ["/sizing-a-battery"]


def test_the_opening_is_the_first_real_paragraph():
    body = "## A heading\n\n[[IMAGE: a battery | یک باتری]]\n\nThe first sentence.\n"
    assert opening_text(body) == "The first sentence."


def test_slugs_are_read_from_whichever_field_the_site_sent():
    entries = [{"slug": "one"}, {"url": "https://example.com/blog/two/"}, {"x": 1}]
    assert slugs_from(entries) == ["one", "https://example.com/blog/two/"]


# ---------------------------------------------------------- the search listing


def test_an_over_long_title_is_sent_back():
    assert "SEO-TITLE-LONG" in check(seo_title="x" * 61)
    assert severity_of("SEO-TITLE-LONG", seo_title="x" * 61) == "major"


def test_a_missing_title_or_description_is_a_defect():
    assert "SEO-TITLE-MISSING" in check(seo_title="")
    assert "SEO-META-MISSING" in check(meta_description="")


def test_a_thin_title_is_reported_but_does_not_block():
    assert severity_of("SEO-TITLE-SHORT", seo_title="Batteries") == "minor"


def test_a_listing_copied_from_the_article_is_flagged():
    assert "SEO-TITLE-COPY" in check(seo_title="Sizing a home battery")
    assert "SEO-META-COPY" in check(
        meta_description="A short guide to picking capacity from real consumption."
    )


# ---------------------------------------------------------------- the page


def test_an_h1_in_the_body_competes_with_the_page_title():
    assert "SEO-H1-IN-BODY" in check(body="# A second top-level heading\n\nText.\n")


def test_a_wall_of_text_with_no_headings_is_a_defect():
    assert "SEO-NO-HEADINGS" in check(body="Just one long paragraph, unbroken.\n")


def test_a_skipped_heading_level_is_reported_but_does_not_block():
    found = defects(draft(body="## Two\n\ntext\n\n#### Four\n\ntext\n"), INDEX, [], CONFIG)
    jumps = [i for i in found if i.issue_id.startswith("SEO-HEADING-JUMP")]
    assert jumps and jumps[0].severity == "minor"


def test_a_malformed_image_marker_would_be_published_as_text():
    body = GOOD_BODY + "\n[[IMAGE: a battery bank on a wall]]\n"
    assert "SEO-IMAGE-MARKER" in check(body=body)


def test_thin_alt_text_is_reported():
    body = GOOD_BODY + "\n[[IMAGE: a battery bank on a wall | باتری]]\n"
    assert "SEO-IMAGE-ALT-1" in check(body=body)


def test_a_lead_image_without_alt_text_is_a_defect():
    assert "SEO-LEAD-ALT" in check(
        featured_image_prompt="a battery bank in a utility room"
    )


# ------------------------------------------------------------------- addresses


def test_a_slug_that_is_already_taken_blocks():
    assert severity_of("SEO-SLUG-TAKEN", slug="sizing-a-battery") == "critical"


def test_a_slug_that_is_not_a_slug_is_sent_back():
    assert "SEO-SLUG-FORMAT" in check(slug="Sizing A Battery!")


def test_an_internal_link_to_nowhere_blocks():
    body = GOOD_BODY + "\nSee [our guide](/articles/a-page-we-never-wrote).\n"
    assert severity_of("SEO-LINK-BROKEN", body=body) == "critical"


def test_links_that_resolve_are_left_alone():
    body = (
        GOOD_BODY
        + "\nSee [the guide](/articles/sizing-a-battery), [about us](/about), "
        + "[the product](https://example.com/products/deye-sun-12k) and "
        + "[the standard](https://iec.ch/62619).\n"
    )
    assert check(body=body) == []


def test_without_site_data_link_checking_is_skipped_rather_than_guessed():
    """An empty index means the fetch failed, not that every link is broken."""
    body = GOOD_BODY + "\nSee [our guide](/articles/anything-at-all).\n"
    found = defects(draft(body=body), SiteIndex(), [], CONFIG)
    assert not [i for i in found if i.issue_id.startswith("SEO-LINK")]


# -------------------------------------------------------------------- keywords


def test_a_missing_keyword_in_the_title_is_reported():
    found = defects(draft(), INDEX, ["depth of discharge"], CONFIG)
    assert "SEO-KEYWORD-TITLE" in ids(found)


def test_a_keyword_that_is_present_passes():
    found = defects(draft(), INDEX, ["home battery", "inverter sizing"], CONFIG)
    assert ids(found) == []


def test_an_opening_that_never_names_the_subject_is_reported():
    """Minor: a reader who cannot tell in one paragraph what this is has left."""
    body = "## A heading\n\nTwo numbers decide it, and neither is obvious.\n"
    found = defects(draft(body=body), INDEX, ["home battery"], CONFIG)
    opening = [i for i in found if i.issue_id == "SEO-KEYWORD-OPENING"]
    assert opening and opening[0].severity == "minor"


def test_a_keyword_in_another_script_is_not_demanded_of_the_title():
    """The one check that would otherwise force English into a Persian title."""
    persian = draft(
        title="اندازه‌گیری ظرفیت باتری خانگی",
        seo_title="ظرفیت باتری خانگی را چطور حساب کنیم",
    )
    found = defects(persian, INDEX, ["home battery sizing"], CONFIG)
    assert not [i for i in found if i.issue_id.startswith("SEO-KEYWORD")]
