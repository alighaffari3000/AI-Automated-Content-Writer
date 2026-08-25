"""Articles as a section of a site rather than a pile of them.

Two failures this catches, both invisible from inside a single article: a new
piece competing with one already published for the same searcher, and a page
nothing links to. Neither is a defect any reviewer could see — they are only
visible from what the site already has.
"""

from __future__ import annotations

import pytest

from app.agent import cannibalises
from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "structure.db"))


def covered(store: Store, title: str, keywords: str) -> None:
    category = store.add_category("An area")
    store.record_topic(category, title, "an angle", keywords)


# ------------------------------------------------------------ cannibalisation


def test_a_subject_whose_every_query_is_taken_is_refused(store):
    covered(store, "Sizing a home battery", "battery sizing, usable capacity")
    assert cannibalises(
        ["battery sizing", "usable capacity"], store.claimed_keywords()
    ) == "Sizing a home battery"


def test_sharing_one_broad_keyword_is_normal(store):
    """Two articles about batteries both mention batteries."""
    covered(store, "Sizing a home battery", "battery sizing, usable capacity")
    assert not cannibalises(
        ["battery sizing", "cold weather performance"], store.claimed_keywords()
    )


def test_a_subject_with_no_keywords_at_all_is_not_judged_a_duplicate(store):
    covered(store, "Sizing a home battery", "battery sizing")
    assert not cannibalises([], store.claimed_keywords())


def test_keywords_are_matched_however_they_were_typed(store):
    covered(store, "Sizing a home battery", "ظرفیت باتری ۸۰ درصد")
    assert cannibalises(["ظرفیت باتری 80 درصد"], store.claimed_keywords())


def test_a_claim_on_a_keyword_belongs_to_the_article_that_took_it_first(store):
    covered(store, "The first article", "peak shaving")
    covered(store, "The second article", "peak shaving")
    assert store.claimed_keywords()[  # newest first, so the later row is seen first
        "peak shaving"
    ] == "The second article"


# -------------------------------------------------------------------- orphans


def test_a_page_nothing_links_to_is_offered_to_the_writer(store):
    article = store.start_article(None)
    store.finish_article(
        article,
        draft={"title": "One", "slug": "one", "body": "See [the guide](/linked-to)."},
        status="draft_sent",
        verdict="APPROVE",
        rounds=1,
    )
    assert store.unlinked(["linked-to", "nobody-links-here"]) == ["nobody-links-here"]


def test_a_slug_that_prefixes_a_linked_one_is_still_an_orphan(store):
    """A link to /inverter-sizing-guide is not a link to /inverter."""
    article = store.start_article(None)
    store.finish_article(
        article,
        draft={
            "title": "One",
            "slug": "one",
            "body": "See [the guide](/inverter-sizing-guide).",
        },
        status="draft_sent",
        verdict="APPROVE",
        rounds=1,
    )
    assert store.unlinked(["inverter", "inverter-sizing-guide"]) == ["inverter"]


def test_with_nothing_written_yet_everything_looks_unlinked(store):
    assert store.unlinked(["a", "b"]) == ["a", "b"]


def test_asking_about_nothing_returns_nothing(store):
    assert store.unlinked([]) == []


# --------------------------------------------------------------------- pillar


def test_a_cluster_can_be_told_what_it_hangs_off(store):
    category = store.add_category("Batteries")
    assert store.set_pillar(category, "Home-Battery-Guide") is True
    assert store.next_category()["pillar_slug"] == "home-battery-guide"


def test_naming_a_pillar_for_a_category_that_does_not_exist_fails(store):
    assert store.set_pillar(999, "anything") is False
