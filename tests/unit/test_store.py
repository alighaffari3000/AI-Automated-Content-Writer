"""The topic bank is permanent — this is what that has to mean in practice."""

from __future__ import annotations

import sqlite3

import pytest

from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


def test_a_written_topic_stays_in_the_bank_and_goes_to_the_back(store):
    first = store.add_topic("Topic A")
    second = store.add_topic("Topic B")

    assert store.next_topic()["id"] == first
    store.mark_topic_used(first)

    # Not consumed — still there, just behind the one never written.
    assert store.next_topic()["id"] == second
    store.mark_topic_used(second)

    # Both written once: the least recent comes round again.
    assert store.next_topic()["id"] == first


def test_the_bank_cycles_evenly_over_many_runs(store):
    ids = [store.add_topic(f"Topic {n}") for n in range(3)]
    written = []
    for _ in range(6):
        topic = store.next_topic()
        written.append(topic["id"])
        store.mark_topic_used(topic["id"])

    assert written == ids + ids, "each topic should come up once per cycle"
    assert all(
        store.next_topic() is not None for _ in range(1)
    ), "the bank never empties"


def test_priority_decides_only_among_topics_never_written(store):
    ordinary = store.add_topic("Ordinary")
    urgent = store.add_topic("Urgent", priority=10)
    assert store.next_topic()["id"] == urgent
    store.mark_topic_used(urgent)
    assert store.next_topic()["id"] == ordinary


def test_a_paused_topic_is_skipped_but_kept(store):
    paused = store.add_topic("Paused")
    active = store.add_topic("Active")
    with store._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE topics SET status = 'paused' WHERE id = ?", (paused,))

    assert store.next_topic()["id"] == active
    store.mark_topic_used(active)
    assert store.next_topic()["id"] == active, "paused stays out of rotation"

    with store._connect() as conn:  # noqa: SLF001
        row = conn.execute("SELECT id FROM topics WHERE id = ?", (paused,)).fetchone()
    assert row is not None, "paused must never mean deleted"


def test_a_failed_run_puts_the_topic_back_at_the_front(store):
    first = store.add_topic("First")
    store.add_topic("Second")

    claimed = store.next_topic()["id"]
    store.mark_topic_used(claimed)
    store.release_topic(claimed)

    assert store.next_topic()["id"] == first
    with store._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT times_used FROM topics WHERE id = ?", (first,)
        ).fetchone()
    assert row["times_used"] == 0, "an aborted run should not count as written"


def test_adding_the_same_title_twice_does_not_duplicate_it(store):
    first = store.add_topic("Same title")
    again = store.add_topic("Same title")
    assert first == again


def test_previous_articles_are_found_for_a_repeated_topic(store):
    topic_id = store.add_topic("Recurring")
    article_id = store.start_article(topic_id)
    store.finish_article(
        article_id,
        draft={"title": "First pass", "slug": "a", "excerpt": "x", "body": "y"},
        status="draft_sent",
        verdict="APPROVE",
        rounds=1,
    )

    previous = store.articles_for_topic(topic_id)
    assert [a["title"] for a in previous] == ["First pass"]


def test_a_database_from_the_consumable_era_keeps_its_topics(tmp_path):
    """Topics used to be consumed once and marked 'used' — gone for good.

    Opening such a database must bring them back into rotation rather than
    leave the bank looking empty.
    """
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE topics (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL UNIQUE,
            notes      TEXT NOT NULL DEFAULT '',
            keywords   TEXT NOT NULL DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'queued',
            priority   INTEGER NOT NULL DEFAULT 0,
            score      REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            used_at    TEXT
        );
        INSERT INTO topics (title, status, created_at, used_at)
        VALUES ('Already written', 'used', '2026-01-01', '2026-01-02'),
               ('Never written',   'queued', '2026-01-01', NULL);
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)

    unwritten = store.next_topic()
    assert unwritten["title"] == "Never written"

    store.mark_topic_used(unwritten["id"])
    revived = store.next_topic()
    assert revived["title"] == "Already written", "an old 'used' topic must return"
    assert revived["times_used"] == 1, "and must count as written once"
