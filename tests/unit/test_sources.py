"""The source audit — the one check that looks outward instead of inward.

Every other check compares the article to the registry. This one compares the
registry to the world: the sources the search really returned, the pages they
really point at, and whether those pages really say what they were quoted as
saying.

The authority lists are configuration, so these tests configure them the way a
site would. A solar site believes deye.com about inverters; a site about
something else has no reason to, and neither does this module by default.
"""

from __future__ import annotations

import pytest

from app.config import SourceConfig
from app.sources import (
    MIN_PAGE_TEXT,
    Source,
    SourceIndex,
    age_in_days,
    audit_fact,
    authority_from,
    classify_domain,
    harvest,
    page_text,
    passage_check,
    passage_state,
    published_date,
)

CONFIG = SourceConfig(
    manufacturers=("deye.com", "victronenergy.com", "enphase.com"),
    publications=("pv-magazine", "pvsyst.com"),
    verify_evidence=True,
    max_age_days=1095,
)
AUTHORITY = authority_from(CONFIG)


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


def new_index() -> SourceIndex:
    return SourceIndex(authority=AUTHORITY)


def index_with(*specs: tuple[str, str, bool]) -> SourceIndex:
    """Build an index of (url, domain, reachable)."""
    index = new_index()
    for url, domain, reachable in specs:
        source = index.add(url=url, domain=domain)
        source.reachable = reachable
    return index


def page(text: str) -> str:
    """Enough text that a page counts as readable rather than as a paywall."""
    return text + " filler. " * ((MIN_PAGE_TEXT // 9) + 1)


# ---------------------------------------------------------------- harvesting


def test_urls_come_from_grounding_not_from_the_model_text():
    events = [
        FakeEvent([FakeChunk(FakeWeb("https://deye.com/spec", "Deye spec", "deye.com"))]),
        FakeEvent(None),
    ]
    index = harvest(events, new_index())
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
    source = harvest(events, new_index()).get("src-1")
    assert source.domain == "enphase.com"
    assert source.tier == 3, "and ranked as the manufacturer it is"


def test_a_title_that_is_prose_is_not_mistaken_for_a_host():
    events = [FakeEvent([FakeChunk(FakeWeb("https://x.test/a", title="How batteries age"))])]
    assert harvest(events, new_index()).get("src-1").domain == "x.test"


def test_the_same_url_across_events_is_one_source():
    chunk = FakeChunk(FakeWeb("https://iec.ch/x", "IEC", "iec.ch"))
    index = harvest([FakeEvent([chunk]), FakeEvent([chunk])], new_index())
    assert len(index.sources) == 1


def test_events_without_grounding_are_ignored_not_fatal():
    assert len(harvest([FakeEvent(None), FakeEvent([])], new_index()).sources) == 0


def test_an_index_survives_a_round_trip_through_state():
    index = index_with(("https://iec.ch/x", "iec.ch", True))
    index.get("src-1").published = "2024-01-02"
    restored = SourceIndex.from_list(index.to_list(), AUTHORITY)
    assert restored.get("src-1").tier == 5
    assert restored.get("src-1").published == "2024-01-02"
    assert restored.get("src-1").reachable is True


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
    assert classify_domain(domain, AUTHORITY)[0] == expected_tier


def test_an_aggregator_never_outranks_a_manufacturer():
    assert (
        classify_domain("reddit.com", AUTHORITY)[0]
        < classify_domain("deye.com", AUTHORITY)[0]
    )


def test_a_manufacturer_nobody_configured_is_only_the_general_web():
    """The lists are per-site on purpose: this module cannot know the industry."""
    assert classify_domain("deye.com")[0] == 1
    assert classify_domain("mit.edu")[0] == 4, "but a university is one anywhere"


# ------------------------------------------------------------ reading a page


def test_scripts_and_markup_are_not_read_as_prose():
    raw = "<p>Real text</p><script>var x = 'not text';</script><style>a{}</style>"
    assert page_text(raw) == "Real text"


def test_entities_come_back_as_the_characters_they_stand_for():
    assert page_text("<p>80&nbsp;% &amp; more</p>").replace("\xa0", " ") == "80 % & more"


@pytest.mark.parametrize(
    "raw",
    [
        '<meta property="article:published_time" content="2024-03-05T10:00:00Z">',
        '<meta content="2024-03-05" name="datePublished">',
        '<script type="application/ld+json">{"datePublished": "2024-03-05"}</script>',
        '<time datetime="2024-03-05T08:00:00+03:30">a while ago</time>',
    ],
)
def test_a_publication_date_is_found_in_whichever_convention_the_page_uses(raw):
    assert published_date(raw) == "2024-03-05"


def test_a_page_with_no_date_is_undated_rather_than_fresh():
    assert published_date("<p>no date here</p>") == ""
    assert age_in_days("") is None


# ------------------------------------------------------- does it say that?


def source_with(text: str, **kwargs) -> Source:
    source = Source(short_id="src-1", url="https://x.test/a", **kwargs)
    source.text = text
    return source


def test_a_passage_quoted_word_for_word_is_found():
    evidence = "LFP chemistry tolerates 80-100% DoD without measurable cycle-life penalty"
    assert passage_state(evidence, source_with(page(f"Intro. {evidence}. More."))) == "quoted"


def test_different_digits_and_punctuation_are_still_the_same_passage():
    """The claim arrives in Persian digits; the page is written in ASCII."""
    assert (
        passage_state(
            "باتری‌های LFP تا ۸۰ درصد تخلیه می‌شوند",
            source_with(page("در عمل باتری های LFP تا 80 درصد تخلیه می شوند و بیشتر")),
        )
        == "quoted"
    )


def test_a_reworded_passage_whose_figures_hold_up_counts_as_support():
    state = passage_state(
        "LFP batteries support 80 to 100 percent depth of discharge safely",
        source_with(page("Depth of discharge for LFP runs from 80 to 100 percent.")),
    )
    assert state == "paraphrased"


def test_a_passage_that_is_simply_not_there_is_absent():
    state = passage_state(
        "LFP batteries deliver exactly 12000 cycles at 45 degrees",
        source_with(page("This page is about mounting rails and roof hooks.")),
    )
    assert state == "absent"


def test_a_figure_written_with_a_persian_decimal_point_is_the_same_figure():
    """A Persian decimal point and an ASCII one write the same number.

    This is what stopped a whole morning's run: every claim resting on a
    decimal was reported as absent from pages that stated it plainly. The two
    digits below look alike on purpose — that is the whole test.
    """
    state = passage_state(
        "ظرفیت باتری ۵.۱۲ کیلووات‌ساعت است و برای بار شبانه کافی است",  # noqa: RUF001
        source_with(
            page("این باتری با ظرفیت ۵/۱۲ کیلووات ساعت برای بار شبانه کافی است")  # noqa: RUF001
        ),
    )
    assert state != "absent"


def test_a_model_number_is_not_a_figure_a_page_has_to_confirm():
    """No page can state the `01` inside SUN-6K-OG01LP1-EU-AM2.

    Reading one out of a part number made every claim that named a product
    permanently unverifiable, which is the opposite of what the check is for.
    """
    state = passage_state(
        "the SUN-6K-OG01LP1-EU-AM2 inverter is rated at 6 kW continuous",
        source_with(page("The SUN-6K-OG01LP1-EU-AM2 is rated 6 kW continuous output.")),
    )
    assert state in {"quoted", "paraphrased"}


def test_a_real_figure_stuck_to_its_unit_is_still_checked():
    """Dropping identifiers must not quietly drop `5kWh` with them."""
    state = passage_state(
        "the pack stores 5kWh which covers the evening load",
        source_with(page("This pack stores 9kWh and covers the evening load easily.")),
    )
    assert state == "absent"


def test_an_absent_passage_says_how_near_it_came():
    """A stopped run has to name its own cause, or someone reads the database.

    Most of the words present and one figure missing is a different failure
    from nothing matching at all, and only the first is worth re-checking.
    """
    check = passage_check(
        "the battery stores 5.12 kilowatt hours for the evening load",
        source_with(page("The battery stores 9.99 kilowatt hours for the evening load.")),
    )
    assert check.state == "absent"
    assert check.missing_figures == ("5.12",)
    assert "5.12" in check.why()


def test_a_page_too_thin_to_read_proves_nothing():
    """A paywall or a page built in the browser is not evidence of absence."""
    assert passage_state("anything at all", source_with("Subscribe to continue")) == "unchecked"


def test_a_registry_source_is_not_re_read_every_morning():
    known = Source(short_id="reg-1", url="https://deye.com/x", from_registry=True)
    assert passage_state("a passage nobody fetched", known) == "quoted"


# ------------------------------------------------------------------- audit


def test_a_fact_citing_a_source_that_was_never_returned_is_rejected():
    """The model can invent a citation; it cannot invent it into the index."""
    index = index_with(("https://deye.com/a", "deye.com", True))
    audit = audit_fact(["src-99"], index, "HIGH")
    assert audit.allowed is False
    assert "never returned" in audit.note


def test_a_fact_with_no_citation_at_all_is_rejected():
    audit = audit_fact([], index_with(), "HIGH")
    assert audit.allowed is False
    assert audit.confidence == "LOW"


def test_a_manufacturer_source_keeps_its_confidence():
    index = index_with(("https://deye.com/spec", "deye.com", True))
    audit = audit_fact(["src-1"], index, "HIGH")
    assert (audit.confidence, audit.allowed, audit.note) == ("HIGH", True, "")


def test_a_confident_claim_on_only_general_web_is_downgraded():
    """Exactly the failure the pipeline exists to prevent."""
    index = index_with(("https://some-blog.ir/post", "some-blog.ir", True))
    audit = audit_fact(["src-1"], index, "HIGH")
    assert audit.confidence == "MEDIUM"
    assert audit.allowed is True
    assert "general-web" in audit.note


def test_the_strongest_cited_source_decides():
    index = index_with(
        ("https://blog.ir/x", "blog.ir", True),
        ("https://iec.ch/y", "iec.ch", True),
    )
    audit = audit_fact(["src-1", "src-2"], index, "HIGH")
    assert (audit.confidence, audit.allowed) == ("HIGH", True)


def test_unreachable_sources_lower_a_claim_rather_than_killing_the_run():
    """This pipeline runs from networks where half the internet is a coin flip."""
    index = index_with(("https://deye.com/gone", "deye.com", False))
    audit = audit_fact(["src-1"], index, "MEDIUM")
    assert audit.confidence == "LOW"
    assert audit.allowed is True, "a medium claim survives, weakened"
    assert "unreachable" in audit.note


def test_a_high_confidence_claim_on_only_unreachable_sources_is_not_publishable():
    index = index_with(("https://deye.com/gone", "deye.com", False))
    audit = audit_fact(["src-1"], index, "HIGH")
    assert (audit.confidence, audit.allowed) == ("LOW", False)


def test_a_claim_whose_passage_is_missing_from_its_own_source_is_not_publishable():
    """The failure no reviewer can catch: article and registry agree perfectly,
    and the page they both rest on never said it."""
    index = index_with(("https://deye.com/spec", "deye.com", True))
    index.get("src-1").text = page("This page is about roof mounting only.")
    audit = audit_fact(
        ["src-1"], index, "HIGH", evidence="The inverter sustains 12 kW continuously"
    )
    assert (audit.confidence, audit.allowed) == ("LOW", False)
    assert "not found on the page" in audit.note
    assert audit.verified is False


def test_a_verified_passage_is_marked_as_such():
    index = index_with(("https://deye.com/spec", "deye.com", True))
    evidence = "The inverter sustains 12 kW continuously"
    index.get("src-1").text = page(f"Specifications. {evidence}. Weight 40 kg.")
    audit = audit_fact(["src-1"], index, "HIGH", evidence=evidence)
    assert audit.verified is True
    assert (audit.confidence, audit.allowed) == ("HIGH", True)


def test_one_source_that_does_have_the_passage_is_enough():
    index = index_with(
        ("https://blog.ir/x", "blog.ir", True),
        ("https://deye.com/spec", "deye.com", True),
    )
    evidence = "The inverter sustains 12 kW continuously"
    index.get("src-1").text = page("Unrelated prose about panels.")
    index.get("src-2").text = page(f"{evidence} at 25 degrees.")
    audit = audit_fact(["src-1", "src-2"], index, "HIGH", evidence=evidence)
    assert audit.allowed is True and audit.verified is True


def test_an_old_trade_publication_is_treated_as_possibly_stale():
    index = index_with(("https://pv-magazine.com/x", "pv-magazine.com", True))
    index.get("src-1").age_days = 1500
    audit = audit_fact(["src-1"], index, "HIGH", config=CONFIG)
    assert audit.confidence == "MEDIUM"
    assert "1500 days old" in audit.note


def test_age_does_not_count_against_a_standard_or_a_datasheet():
    """An old datasheet is still the datasheet; an old market figure is a guess."""
    index = index_with(("https://iec.ch/62619", "iec.ch", True))
    index.get("src-1").age_days = 4000
    audit = audit_fact(["src-1"], index, "HIGH", config=CONFIG)
    assert audit.confidence == "HIGH"


def test_a_registry_fact_audits_on_the_authority_it_earned_before():
    index = new_index()
    index.add_known(
        "reg-1",
        "https://deye.com/spec",
        tier=3,
        tier_label="manufacturer documentation",
        verified_at="2026-07-01",
    )
    audit = audit_fact(["reg-1"], index, "HIGH", evidence="a passage nobody re-read")
    assert (audit.confidence, audit.allowed, audit.verified) == ("HIGH", True, True)


def test_registry_sources_do_not_renumber_the_search_results():
    index = new_index()
    index.add(url="https://iec.ch/a", domain="iec.ch")
    index.add_known("reg-1", "https://deye.com/x", tier=3)
    index.add(url="https://deye.com/b", domain="deye.com")
    assert sorted(index.sources) == ["reg-1", "src-1", "src-2"]
