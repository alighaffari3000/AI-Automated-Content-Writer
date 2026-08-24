"""Categories are what a person maintains; subjects are invented per run.

These pin down the part that has to be fair and permanent: whose turn it is,
and that nothing a person defined ever disappears on its own.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


def test_coverage_stays_even_across_subject_areas(store):
    ids = [store.add_category(f"Area {n}") for n in range(3)]
    order = []
    for _ in range(6):
        category = store.next_category()
        order.append(category["id"])
        store.mark_category_used(category["id"])

    assert order == ids + ids, "every area is written once before any is written twice"


def test_a_category_is_never_consumed(store):
    only = store.add_category("The only area")
    for _ in range(3):
        assert store.next_category()["id"] == only
        store.mark_category_used(only)

    assert store.next_category() is not None, "the rotation never runs dry"


def test_a_paused_area_is_skipped_but_kept(store):
    paused = store.add_category("Paused")
    active = store.add_category("Active")
    with store._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE categories SET status = 'paused' WHERE id = ?", (paused,))

    assert store.next_category()["id"] == active
    store.mark_category_used(active)
    assert store.next_category()["id"] == active

    with store._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT id FROM categories WHERE id = ?", (paused,)
        ).fetchone()
    assert row is not None, "paused must never mean deleted"


def test_a_failed_run_does_not_cost_the_area_its_turn(store):
    first = store.add_category("First")
    store.add_category("Second")

    claimed = store.next_category()["id"]
    store.mark_category_used(claimed)
    store.release_category(claimed)

    assert store.next_category()["id"] == first
    with store._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT times_used FROM categories WHERE id = ?", (first,)
        ).fetchone()
    assert row["times_used"] == 0


def test_defining_the_same_area_twice_does_not_duplicate_it(store):
    assert store.add_category("Same name") == store.add_category("Same name")


def test_seeding_gives_a_fresh_install_something_to_write_about(store):
    added = store.seed_default_categories()
    assert added >= 5
    assert store.next_category() is not None
    assert store.seed_default_categories() == 0, "seeding twice adds nothing"


def test_the_planner_can_see_what_it_already_covered(store):
    area = store.add_category("Batteries")
    store.record_topic(area, "Depth of discharge explained", "the three numbers", "dod")
    store.record_topic(area, "Why LFP replaced NMC", "safety and cycle life", "lfp")

    covered = store.topics_in_category(area)
    assert [t["title"] for t in covered] == [
        "Why LFP replaced NMC",
        "Depth of discharge explained",
    ]


def test_subjects_are_kept_per_area_not_pooled(store):
    batteries = store.add_category("Batteries")
    inverters = store.add_category("Inverters")
    store.record_topic(batteries, "A battery subject")
    store.record_topic(inverters, "An inverter subject")

    assert [t["title"] for t in store.topics_in_category(batteries)] == [
        "A battery subject"
    ]


def test_a_database_from_the_hand_stocked_era_keeps_its_history(tmp_path):
    """Topics used to be a queue a person filled. Those rows are history now.

    They must survive the change, because what was already written is exactly
    what the planner needs in order not to write it again.
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
        INSERT INTO topics (title, status, created_at)
        VALUES ('Something written back then', 'used', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    with store._connect() as reopened:  # noqa: SLF001
        row = reopened.execute("SELECT title, angle FROM topics").fetchone()
    assert row["title"] == "Something written back then"
    assert row["angle"] == ""
