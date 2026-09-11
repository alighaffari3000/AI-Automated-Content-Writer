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
