"""What a second research pass is told about the first.

The failure behind this: a run whose every fact failed the audit stopped with
"no source solid enough to write from". Retrying is only worth the search if
the second pass learns what the first got wrong, so what is handed back has to
carry the audit's actual verdict — the passage, the page, and the reason.
"""

from __future__ import annotations

from app.config import ContentConfig
from app.prompts import (
    RETRY_RULE,
    fact_builder_instruction,
    format_rejected_facts,
    researcher_instruction,
)
from app.schemas import Fact
from app.sources import FactAudit


def rejected(**overrides) -> Fact:
    base = {
        "fact_id": "FACT-001",
        "claim": "The inverter supports 9600 W of PV input.",
        "source": "a guide",
        "source_ids": ["src-4"],
        "source_url": "https://example.com/guide",
        "evidence": "maximum PV input power of 9600 W",
        "confidence": "LOW",
        "allowed": False,
        "audit_note": "the quoted passage was not found on the page it cites",
    }
    base.update(overrides)
    return Fact(**base)


def test_the_audit_s_reason_reaches_the_next_attempt():
    text = format_rejected_facts([rejected()])
    assert "the quoted passage was not found on the page it cites" in text
    assert "maximum PV input power of 9600 W" in text
    assert "https://example.com/guide" in text


def test_a_fact_that_survived_is_not_reported_as_a_failure():
    text = format_rejected_facts([rejected(), rejected(fact_id="FACT-002", allowed=True)])
    assert text.count("FACT") == 0  # ids are not the point; claims are
    assert len([line for line in text.splitlines() if line.startswith("- ")]) == 1


def test_registering_nothing_at_all_is_reported_as_its_own_failure():
    """Empty notes and rejected notes are different mistakes, and a retry told
    the wrong one searches for the wrong thing."""
    text = format_rejected_facts([])
    assert "no checkable claim" in text


def test_both_research_agents_are_told_what_was_thrown_out():
    """The researcher finds the sources and the fact builder picks which one
    each claim cites. The last attempt's verdict bears on both."""
    content = ContentConfig()
    for instruction in (
        researcher_instruction(content),
        fact_builder_instruction(content),
    ):
        assert "{rejected_facts?}" in instruction
        assert RETRY_RULE.splitlines()[0] in instruction


# ------------------------------------------------- what an empty registry does


class FakeContext:
    """Just the state bag: persist_registry reads nothing else off the context."""

    def __init__(self, **state):
        self.state = dict(state)


def empty_registry_run(monkeypatch, attempt: int):
    """One audit that accepts nothing, at a given attempt number."""
    from app import agent

    # The subject of the test is the routing, not the audit that produced the
    # verdict or the registry that outlives it.
    monkeypatch.setattr(agent, "fetch_pages", lambda *a, **k: None)
    monkeypatch.setattr(agent, "remember", lambda *a, **k: None)
    monkeypatch.setattr(
        agent,
        "audit_fact",
        lambda *a, **k: FactAudit(
            "LOW", False, "the quoted passage was not found on the page it cites"
        ),
    )
    bundle = {
        "angle": "an angle",
        "outline": ["one"],
        "facts": [rejected(allowed=True).model_dump()],
    }
    ctx = FakeContext(
        research_bundle=bundle,
        source_index=[],
        article_id=0,
        research_attempt=attempt,
    )
    return agent.persist_registry(ctx, None)


def test_the_first_empty_registry_goes_back_to_research(monkeypatch):
    """The failure this exists for: a run that stopped at the first empty
    registry, when the audit cannot tell an undocumented subject from a
    research pass that quoted from memory."""
    event = empty_registry_run(monkeypatch, attempt=0)
    assert event.actions.route == "retry"
    assert event.actions.state_delta["research_attempt"] == 1
    assert "not found on the page it cites" in event.actions.state_delta["rejected_facts"]


def test_the_second_empty_registry_stops_rather_than_looping(monkeypatch):
    """A subject the open web does not document still has to end the run."""
    event = empty_registry_run(monkeypatch, attempt=1)
    assert event.actions.route == "no_facts"


# ------------------------------------------------ a registry with no numbers


def threshold(monkeypatch, n: int) -> None:
    """Settings are frozen, so the whole tree is swapped rather than one field."""
    from dataclasses import replace

    from app import agent

    monkeypatch.setattr(
        agent,
        "settings",
        replace(agent.settings, content=replace(agent.settings.content, min_numeric_facts=n)),
    )


def numeric_shortfall_run(monkeypatch, attempt: int, claims: list[str]):
    """One audit that accepts everything, over facts with the given claims."""
    from app import agent

    monkeypatch.setattr(agent, "fetch_pages", lambda *a, **k: None)
    monkeypatch.setattr(agent, "remember", lambda *a, **k: None)
    monkeypatch.setattr(agent, "audit_fact", lambda *a, **k: FactAudit("HIGH", True))
    threshold(monkeypatch, 3)
    facts = [
        rejected(fact_id=f"FACT-{n:03d}", claim=c, allowed=True).model_dump()
        for n, c in enumerate(claims, start=1)
    ]
    ctx = FakeContext(
        research_bundle={"angle": "a", "outline": ["one"], "facts": facts},
        source_index=[],
        article_id=0,
        research_attempt=attempt,
    )
    return agent.persist_registry(ctx, None)


PROSE = [
    "LFP tolerates deep discharge well.",
    "Lead-acid ages quickly when discharged deeply.",
    "Depth of discharge decides usable capacity.",
]
FIGURES = [
    "LFP supports 80% to 100% depth of discharge.",
    "Lead-acid is limited to about 50% depth of discharge.",
    "A 5 kWh battery at 80% yields 4 kWh usable.",
]


def test_a_registry_of_prose_goes_back_to_research(monkeypatch):
    """The failure behind six of the first eight articles: every fact true,
    none of them a number, and a sizing guide with no arithmetic in it."""
    event = numeric_shortfall_run(monkeypatch, attempt=0, claims=PROSE)
    assert event.actions.route == "retry"
    fed_back = event.actions.state_delta["rejected_facts"]
    assert "only 0 carry a figure" in fed_back
    assert "LFP tolerates deep discharge well." in fed_back


def test_enough_figures_goes_straight_to_the_writer(monkeypatch):
    event = numeric_shortfall_run(monkeypatch, attempt=0, claims=FIGURES)
    assert event.actions.route == "ok"


def test_short_on_numbers_twice_writes_anyway(monkeypatch):
    """Unlike an empty registry, a thin one is worth an article: the gate
    counts the missing arithmetic and a person sees it marked."""
    event = numeric_shortfall_run(monkeypatch, attempt=1, claims=PROSE)
    assert event.actions.route == "ok"


def test_the_threshold_can_be_switched_off(monkeypatch):
    from app import agent

    event = numeric_shortfall_run(monkeypatch, attempt=0, claims=PROSE)
    assert event.actions.route == "retry"
    threshold(monkeypatch, 0)
    monkeypatch.setattr(agent, "audit_fact", lambda *a, **k: FactAudit("HIGH", True))
    ctx = FakeContext(
        research_bundle={
            "angle": "a",
            "outline": ["one"],
            "facts": [rejected(claim=PROSE[0], allowed=True).model_dump()],
        },
        source_index=[],
        article_id=0,
        research_attempt=0,
    )
    assert agent.persist_registry(ctx, None).actions.route == "ok"
