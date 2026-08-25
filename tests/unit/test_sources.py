"""The source audit — the one check that looks outward instead of inward.

Every other check compares the article to the registry. This one compares the
registry to what the search actually returned, so a claim cannot be true here
merely because a model asserted it twice.
"""

from __future__ import annotations

import pytest

from app.sources import Source, SourceIndex, audit_fact, classify_domain, harvest


class FakeWeb:
    def __init__(self, uri, title="", domain=""):
        self.uri, self.title, self.domain = uri, title, domain


class FakeChunk:
    def __init__(self, web):
        self.web = web


class FakeMetadata:
    def __init__(self, chunks):
        self.grounding_chunks = chunks


class FakeEvent:
    def __init__(self, chunks=None):
        self.grounding_metadata = FakeMetadata(chunks) if chunks else None


def index_with(*specs: tuple[str, str, bool]) -> SourceIndex:
    """Build an index of (url, domain, reachable)."""
    index = SourceIndex()
    for url, domain, reachable in specs:
        source = index.add(url=url, domain=domain)
        source.reachable = reachable
    return index


# ---------------------------------------------------------------- harvesting


def test_urls_come_from_grounding_not_from_the_model_text():
    events = [
        FakeEvent([FakeChunk(FakeWeb("https://deye.com/spec", "Deye spec", "deye.com"))]),
        FakeEvent(None),
    ]
    index = harvest(events)
    assert [s.url for s in index.sources.values()] == ["https://deye.com/spec"]
    assert index.get("src-1").tier == 3


def test_the_publisher_is_read_from_the_title_where_grounding_puts_it():
    """`domain` comes back None; the host is in `title`. Miss this and every
    source ranks as general web, so the audit downgrades everything equally."""
    events = [
        FakeEvent([
            FakeChunk(FakeWeb(
                "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
                title="enphase.com",
                domain="",
            ))
        ])
    ]
    source = harvest(events).get("src-1")
    assert source.domain == "enphase.com"
    assert source.tier == 3, "and ranked as the manufacturer it is"


def test_a_title_that_is_prose_is_not_mistaken_for_a_host():
    events = [FakeEvent([FakeChunk(FakeWeb("https://x.test/a", title="How batteries age"))])]
    assert harvest(events).get("src-1").domain == "x.test"


def test_a_known_manufacturer_in_the_title_ranks_as_one():
    events = [
        FakeEvent([
            FakeChunk(FakeWeb(
                "https://vertexaisearch.cloud.google.com/grounding-api-redirect/z",
                title="deye.com",
            ))
        ])
    ]
    assert harvest(events).get("src-1").tier == 3


def test_the_same_url_across_events_is_one_source():
    chunk = FakeChunk(FakeWeb("https://iec.ch/x", "IEC", "iec.ch"))
    index = harvest([FakeEvent([chunk]), FakeEvent([chunk])])
    assert len(index.sources) == 1


def test_events_without_grounding_are_ignored_not_fatal():
    assert len(harvest([FakeEvent(None), FakeEvent([])]).sources) == 0


# --------------------------------------------------------------- authority


@pytest.mark.parametrize(
    ("domain", "expected_tier"),
    [
        ("webstore.iec.ch", 5),
        ("www.nrel.gov", 5),
        ("mit.edu", 4),
        ("www.deye.com", 3),
        ("pv-magazine.com", 2),
        ("www.pvsyst.com", 2),
        ("en.wikipedia.org", 1),
        ("some-shop.ir", 1),
    ],
)
def test_domains_rank_by_who_would_actually_know(domain, expected_tier):
    assert classify_domain(domain)[0] == expected_tier


def test_an_aggregator_never_outranks_a_manufacturer():
    assert classify_domain("reddit.com")[0] < classify_domain("deye.com")[0]


# ------------------------------------------------------------------- audit


def test_a_fact_citing_a_source_that_was_never_returned_is_rejected():
    """The model can invent a citation; it cannot invent it into the index."""
    index = index_with(("https://deye.com/a", "deye.com", True))
    confidence, allowed, note = audit_fact(["src-99"], index, "HIGH")
    assert allowed is False
    assert "never returned" in note


def test_a_fact_with_no_citation_at_all_is_rejected():
    confidence, allowed, note = audit_fact([], index_with(), "HIGH")
    assert allowed is False
    assert confidence == "LOW"


def test_a_manufacturer_source_keeps_its_confidence():
    index = index_with(("https://deye.com/spec", "deye.com", True))
    confidence, allowed, note = audit_fact(["src-1"], index, "HIGH")
    assert (confidence, allowed, note) == ("HIGH", True, "")


def test_a_confident_claim_on_only_general_web_is_downgraded():
    """Exactly the failure the pipeline exists to prevent."""
    index = index_with(("https://some-blog.ir/post", "some-blog.ir", True))
    confidence, allowed, note = audit_fact(["src-1"], index, "HIGH")
    assert confidence == "MEDIUM"
    assert allowed is True
    assert "general-web" in note


def test_the_strongest_cited_source_decides():
    index = index_with(
        ("https://blog.ir/x", "blog.ir", True),
        ("https://iec.ch/y", "iec.ch", True),
    )
    confidence, allowed, _ = audit_fact(["src-1", "src-2"], index, "HIGH")
    assert (confidence, allowed) == ("HIGH", True)


def test_unreachable_sources_lower_a_claim_rather_than_killing_the_run():
    """This pipeline runs from networks where half the internet is a coin flip."""
    index = index_with(("https://deye.com/gone", "deye.com", False))
    confidence, allowed, note = audit_fact(["src-1"], index, "MEDIUM")
    assert confidence == "LOW"
    assert allowed is True, "a medium claim survives, weakened"
    assert "unreachable" in note


def test_a_high_confidence_claim_on_only_unreachable_sources_is_not_publishable():
    index = index_with(("https://deye.com/gone", "deye.com", False))
    confidence, allowed, _ = audit_fact(["src-1"], index, "HIGH")
    assert (confidence, allowed) == ("LOW", False)
