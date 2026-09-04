"""The registry: what stays true, and for how long.

A fact reused is a fact nobody checked today, so the two things worth pinning
down are which claims are allowed in at all, and that a claim past its shelf
life is never offered again — silently reusing a stale price is a worse failure
than never having stored it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import RegistryConfig
from app.normalize import claim_key
from app.store import Store

TTL = RegistryConfig(ttl_days={"price": 7, "specification": 180, "general": 90})


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "registry.db"))


def fact(claim: str, **overrides) -> dict:
    base = {
        "claim": claim,
        "kind": "specification",
        "source": "Manufacturer documentation",
        "source_url": "https://deye.com/spec",
        "evidence": "the passage",
        "confidence": "HIGH",
        "allowed": True,
        "verified": True,
        "tier": 3,
        "tier_label": "manufacturer documentation",
    }
    base.update(overrides)
    return base


def remember(store: Store, *facts: dict) -> int:
    return store.remember_facts(1, list(facts), TTL.shelf_life)


def expire(store: Store, claim: str, days_ago: int = 1) -> None:
    """Age one row past its shelf life, the way a week of waiting would."""
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with store._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE facts SET expires_at = ? WHERE claim_key = ?",
            (stamp, claim_key(claim)),
        )


# --------------------------------------------------------------- what is kept


def test_a_verified_claim_is_remembered(store):
    assert remember(store, fact("LFP batteries tolerate 80 percent depth of discharge")) == 1
    assert len(store.live_facts(["depth", "discharge"])) == 1


def test_a_claim_the_audit_rejected_is_never_remembered(store):
    assert remember(store, fact("A rejected claim about batteries", allowed=False)) == 0


def test_an_unverified_claim_from_a_weak_source_is_not_remembered(store):
    """Reuse means nobody checks it again, so weak and unchecked cannot enter."""
    assert remember(store, fact("A blog said batteries", verified=False, tier=1)) == 0


def test_an_unverified_claim_from_an_authority_is_remembered(store):
    """The best sources are often the least readable — a datasheet is a PDF."""
    assert remember(store, fact("The datasheet states 12 kW", verified=False, tier=3)) == 1


def test_the_same_claim_in_different_words_updates_one_row(store):
    remember(store, fact("LFP batteries tolerate 80 percent DoD", confidence="MEDIUM"))
    remember(store, fact("LFP  batteries tolerate 80 percent DoD!", confidence="HIGH"))
    rows = store.live_facts(["batteries"])
    assert len(rows) == 1, "one answer per question, not two opinions"
    assert rows[0]["confidence"] == "HIGH"


def test_a_claim_that_differs_by_a_number_is_a_different_claim(store):
    """Which is the case that matters: a specification that changed."""
    remember(store, fact("The inverter sustains 12 kW"))
    remember(store, fact("The inverter sustains 15 kW"))
    assert len(store.live_facts(["inverter"])) == 2


# ------------------------------------------------------------------ shelf life


def test_an_expired_claim_is_not_offered_again(store):
    remember(store, fact("The price is 1850 dollars", kind="price"))
    expire(store, "The price is 1850 dollars")
    assert store.live_facts(["price"]) == []


def test_an_expired_claim_is_kept_rather_than_deleted(store):
    """It is the signal that the article resting on it has gone stale."""
    remember(store, fact("The price is 1850 dollars", kind="price"))
    expire(store, "The price is 1850 dollars")
    assert store.fact_stats()["expired"] == 1
    assert len(store.recent_facts(live_only=False)) == 1


def test_shelf_life_follows_the_kind_of_claim(store):
    remember(
        store,
        fact("The price is 1850 dollars", kind="price"),
        fact("The cell chemistry is LFP", kind="specification"),
    )
    rows = {r["kind"]: r["expires_at"] for r in store.recent_facts()}
    assert rows["price"] < rows["specification"], "a price goes stale first"


# ------------------------------------------------------------------ recall


def test_recall_is_by_what_the_topic_is_about(store):
    remember(
        store,
        fact("LFP batteries tolerate 80 percent depth of discharge"),
        fact("Roof hooks must be rated for snow load"),
    )
    found = store.live_facts(["batteries", "discharge"])
    assert [r["claim"] for r in found] == [
        "LFP batteries tolerate 80 percent depth of discharge"
    ]


def test_the_most_relevant_claim_comes_first(store):
    remember(
        store,
        fact("Batteries age with heat"),
        fact("Batteries age with heat and depth of discharge together"),
    )
    found = store.live_facts(["batteries", "depth", "discharge"])
    assert "depth of discharge" in found[0]["claim"]


def test_a_topic_with_nothing_stored_gets_nothing(store):
    remember(store, fact("LFP batteries tolerate 80 percent DoD"))
    assert store.live_facts(["mounting", "rails"]) == []
    assert store.live_facts([]) == []


def test_persian_and_ascii_digits_find_the_same_claim(store):
    remember(store, fact("عمق تخلیه باتری تا ۸۰ درصد است"))
    assert len(store.live_facts(["عمق", "تخلیه"])) == 1


# --------------------------------------------------------------- housekeeping


def test_reuse_is_counted(store):
    claim = "LFP batteries tolerate 80 percent DoD"
    remember(store, fact(claim))
    store.mark_facts_used([claim_key(claim)])
    store.mark_facts_used([claim_key(claim)])
    assert store.recent_facts()[0]["times_used"] == 2
    assert store.fact_stats()["reuses"] == 2


def reuse_run(store, monkeypatch, claim: str, source_ids: list[str], reworded: str = ""):
    """One pipeline `remember()` pass over a fact that cites the registry."""
    from types import SimpleNamespace

    from app import agent
    from app.schemas import Fact, ResearchBundle
    from app.sources import SourceIndex

    monkeypatch.setattr(agent, "_store", store)
    index = SourceIndex()
    index.add_known("reg-1", "https://deye.com/spec", tier=3)
    index.add(url="https://fresh.example/page", domain="fresh.example")
    bundle = ResearchBundle(
        angle="an angle",
        outline=["one"],
        facts=[
            Fact(
                fact_id="FACT-001",
                claim=reworded or claim,
                source="Deye datasheet",
                source_ids=source_ids,
                evidence="the passage",
                confidence="HIGH",
                kind="specification",
            )
        ],
    )
    state = {
        "article_id": 2,
        "known_facts": [{"reg_id": "reg-1", "claim_key": claim_key(claim)}],
    }
    agent.remember(bundle, index, SimpleNamespace(state=state))


def test_reuse_does_not_push_the_expiry_date_forward(store, monkeypatch):
    """Otherwise a fact reused daily would never expire, and a shelf life that
    renews itself on use is no shelf life at all."""
    claim = "LFP batteries tolerate 80 percent depth of discharge"
    remember(store, fact(claim))
    expires = store.recent_facts()[0]["expires_at"]

    reuse_run(store, monkeypatch, claim, ["reg-1"])

    row = store.recent_facts()[0]
    assert row["expires_at"] == expires, "the shelf life is not renewed by reuse"
    assert row["times_used"] == 1, "but the reuse is counted"


def test_a_fresh_source_riding_along_does_not_renew_the_shelf_life_either(
    store, monkeypatch
):
    """The audit trusts registry sources unread, so `verified` on a mixed
    citation proves nothing about the fresh source — renewing on it would be
    the same no-shelf-life failure through a side door."""
    claim = "LFP batteries tolerate 80 percent depth of discharge"
    remember(store, fact(claim))
    expires = store.recent_facts()[0]["expires_at"]

    reuse_run(store, monkeypatch, claim, ["reg-1", "src-1"])

    assert store.recent_facts()[0]["expires_at"] == expires


def test_a_reworded_reuse_is_still_counted_against_the_stored_row(store, monkeypatch):
    """The count is keyed by the registry's own claim_key, not today's wording."""
    claim = "LFP batteries tolerate 80 percent depth of discharge"
    remember(store, fact(claim))

    reuse_run(
        store,
        monkeypatch,
        claim,
        ["reg-1"],
        reworded="LFP batteries can be discharged to 80 percent of capacity",
    )

    assert store.recent_facts()[0]["times_used"] == 1


def test_a_wrong_fact_can_be_forgotten(store):
    """A registry a person cannot correct is one they stop trusting."""
    remember(store, fact("Something that turned out to be wrong"))
    fact_id = store.recent_facts()[0]["id"]
    assert store.forget_fact(fact_id) is True
    assert store.live_facts(["something"]) == []
    assert store.forget_fact(fact_id) is False


# ------------------------------------------- why a run had nothing to write


def test_a_run_with_nothing_usable_says_why_it_stopped():
    """The stop message has to carry the reason the audit already knows.

    Without it, "no source solid enough" sends whoever reads it to the
    database to find out whether the model invented its citations, the network
    ate the pages, or a real passage went unrecognised.
    """
    from app.agent import diagnose_empty_registry
    from app.schemas import Fact
    from app.sources import SourceIndex

    facts = [
        Fact(
            fact_id="FACT-001",
            claim="the pack stores 5.12 kWh",
            source="a vendor page",
            evidence="the pack stores 5.12 kWh",
            confidence="HIGH",
            allowed=False,
            audit_note=(
                "the quoted passage was not found on the page it cites — "
                "79% of its words are on the page; but it never states 5.12"
            ),
        ),
        Fact(
            fact_id="FACT-002",
            claim="something else entirely",
            source="a vendor page",
            evidence="something else entirely",
            confidence="HIGH",
            allowed=False,
            audit_note="cites no source",
        ),
    ]

    diagnosis = diagnose_empty_registry(facts, SourceIndex())

    assert "2 claim(s) registered, none usable" in diagnosis
    assert "the quoted passage was not found on the page it cites" in diagnosis
    assert "cites no source" in diagnosis
    # The near miss is what says the sources were fine and the check was not.
    assert "never states 5.12" in diagnosis


def test_a_registry_that_registered_nothing_at_all_says_that_instead():
    from app.agent import diagnose_empty_registry
    from app.sources import SourceIndex

    assert "no claims at all" in diagnose_empty_registry([], SourceIndex())
