"""The pipeline's own SQLite database.

Three tables, per the MVP scope: what to write about, what was written, and how
it was judged. Facts are kept as JSON on the article row rather than in their
own table — no reuse and no expiry yet, but every published claim stays
traceable, and a future persistent registry can be seeded from this column.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
-- What the site writes about. This is the permanent part, and the only part a
-- person maintains: the specific subject of each article is invented per run.
CREATE TABLE IF NOT EXISTS categories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    description   TEXT    NOT NULL DEFAULT '',
    audience      TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'active',
    times_used    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    used_at       TEXT
);

-- Every subject the planner has invented, so it can see what it already
-- covered and not propose it again. A log, not a queue — nobody stocks it.
CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id   INTEGER REFERENCES categories(id),
    title         TEXT    NOT NULL,
    angle         TEXT    NOT NULL DEFAULT '',
    keywords      TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'active',
    created_at    TEXT    NOT NULL,
    used_at       TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id      INTEGER REFERENCES topics(id),
    title         TEXT    NOT NULL DEFAULT '',
    slug          TEXT    NOT NULL DEFAULT '',
    excerpt       TEXT    NOT NULL DEFAULT '',
    body          TEXT    NOT NULL DEFAULT '',
    facts_json    TEXT    NOT NULL DEFAULT '[]',
    cost_json     TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'running',
    verdict       TEXT    NOT NULL DEFAULT '',
    rounds        INTEGER NOT NULL DEFAULT 0,
    remote_id     TEXT    NOT NULL DEFAULT '',
    error         TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id    INTEGER NOT NULL REFERENCES articles(id),
    round_number  INTEGER NOT NULL,
    reviewer      TEXT    NOT NULL,
    score         REAL    NOT NULL,
    issues_json   TEXT    NOT NULL DEFAULT '[]',
    summary       TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_article ON reviews(article_id);
CREATE INDEX IF NOT EXISTS idx_categories_status ON categories(status);
"""

# Runs after the migration, because an older database reaches this point with a
# `topics` table that has no category_id to index yet.
LATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_topics_category ON topics(category_id);
"""

# The categories a solar and storage business writes about. Only a starting
# point — they are rows, not code, and `categories add` is how a site says what
# it actually covers.
DEFAULT_CATEGORIES = [
    (
        "Residential solar",
        "Home solar and storage: sizing, choosing equipment, what it costs, "
        "living with it day to day.",
        "homeowners deciding whether and what to buy",
    ),
    (
        "Commercial and industrial",
        "Solar and storage at business scale: peak shaving and demand charges, "
        "factory backup power, microgrids, off-grid sites, energy audits.",
        "plant managers and business owners weighing an investment",
    ),
    (
        "Batteries and energy storage",
        "Storage itself: chemistry, depth of discharge, cycle life, round-trip "
        "efficiency, battery management, temperature, AC- versus DC-coupling.",
        "buyers comparing storage on more than price",
    ),
    (
        "Inverters and equipment",
        "Inverters and the hardware around them: reading a datasheet, surge and "
        "inductive loads, single- versus three-phase, efficiency and MPPT, "
        "cabling and protection.",
        "installers and technically-minded buyers",
    ),
    (
        "Choosing and buying",
        "Decision-making rather than hardware: common design mistakes, what to "
        "ask a supplier, how to compare offers.",
        "anyone about to spend money on a system",
    ),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.executescript(LATE_INDEXES)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring a database made by an earlier version up to date.

        `topics` used to be a hand-stocked queue; it is now a log of subjects
        the planner invented. Old rows are kept as history — they say what was
        written, which is exactly what the planner needs to not repeat itself.
        """
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(topics)")}
        for name, ddl in (
            ("category_id", "INTEGER REFERENCES categories(id)"),
            ("angle", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE topics ADD COLUMN {name} {ddl}")

        article_columns = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
        if "cost_json" not in article_columns:
            conn.execute(
                "ALTER TABLE articles ADD COLUMN cost_json TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "UPDATE topics SET status = 'active' WHERE status IN ('queued', 'used')"
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ categories

    def add_category(
        self, name: str, description: str = "", audience: str = ""
    ) -> int:
        """Define a subject area. Re-adding an existing name is a no-op."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO categories (name, description, audience, created_at)"
                " VALUES (?, ?, ?, ?)",
                (name.strip(), description.strip(), audience.strip(), _now()),
            )
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = conn.execute(
                "SELECT id FROM categories WHERE name = ?", (name.strip(),)
            ).fetchone()
            return int(row["id"]) if row else 0

    def seed_default_categories(self) -> int:
        """Give a fresh installation something to write about.

        Returns how many were actually added, so running it twice reports
        nothing rather than claiming to have added what was already there.
        """
        with self._connect() as conn:
            existing = {
                r["name"] for r in conn.execute("SELECT name FROM categories")
            }
        fresh = [c for c in DEFAULT_CATEGORIES if c[0] not in existing]
        for name, description, audience in fresh:
            self.add_category(name, description, audience)
        return len(fresh)

    def next_category(self) -> dict[str, Any] | None:
        """Whose turn it is today.

        Fewest-articles-written first, so coverage stays even across every
        subject area rather than drifting toward whichever one happens to
        produce topics easily. Ordering by timestamp alone would not hold —
        two runs in the same second tie, and rotation sticks on one row.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM categories WHERE status <> 'paused'"
                " ORDER BY times_used ASC, used_at IS NOT NULL, used_at ASC, id ASC"
                " LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def mark_category_used(self, category_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE categories SET used_at = ?, times_used = times_used + 1"
                " WHERE id = ?",
                (_now(), category_id),
            )

    def release_category(self, category_id: int) -> None:
        """Undo a claim after a failed run, so this category comes up next."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE categories SET used_at = NULL,"
                " times_used = MAX(times_used - 1, 0) WHERE id = ?",
                (category_id,),
            )

    # ---------------------------------------------------------------- topics

    def record_topic(
        self, category_id: int, title: str, angle: str = "", keywords: str = ""
    ) -> int:
        """Log a subject the planner just invented."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO topics (category_id, title, angle, keywords, created_at, used_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (category_id, title.strip(), angle.strip(), keywords.strip(), _now(), _now()),
            )
            return int(cur.lastrowid or 0)

    def discard_topic(self, topic_id: int) -> None:
        """Remove a logged subject that no article ended up existing for.

        The topics table is the planner's memory of what is covered. A subject
        that was claimed and then lost to a failed run is not covered — leaving
        it logged would make the planner avoid a perfectly good subject
        forever.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))

    def recover_abandoned_runs(self, older_than_hours: float = 2.0) -> int:
        """Clean up after runs that died without reaching any exit path.

        A killed process — power cut, Ctrl-C, OOM — leaves an article stuck in
        'running', a category whose turn was consumed for nothing, and a topic
        logged that no article exists for. All three are put back. The age
        guard keeps a genuinely in-flight run (cron overlapping a manual one)
        from being mistaken for a corpse.
        """
        with self._connect() as conn:
            stale = conn.execute(
                "SELECT a.id, a.topic_id, t.category_id FROM articles a"
                " LEFT JOIN topics t ON t.id = a.topic_id"
                " WHERE a.status = 'running'"
                "   AND datetime(a.created_at) < datetime('now', ?)",
                (f"-{older_than_hours} hours",),
            ).fetchall()
            for row in stale:
                conn.execute(
                    "UPDATE articles SET status = 'failed',"
                    " error = 'abandoned: the run died before finishing',"
                    " updated_at = ? WHERE id = ?",
                    (_now(), row["id"]),
                )
                if row["category_id"]:
                    conn.execute(
                        "UPDATE categories SET used_at = NULL,"
                        " times_used = MAX(times_used - 1, 0) WHERE id = ?",
                        (row["category_id"],),
                    )
                if row["topic_id"]:
                    conn.execute(
                        "DELETE FROM topics WHERE id = ?", (row["topic_id"],)
                    )
            return len(stale)

    def topics_in_category(
        self, category_id: int, limit: int = 40
    ) -> list[dict[str, Any]]:
        """What has already been covered here.

        The planner reads this before proposing anything: without it, an
        inventive planner reinvents the same obvious subject every few weeks.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT title, angle, created_at FROM topics"
                " WHERE category_id = ? ORDER BY id DESC LIMIT ?",
                (category_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def recent_titles(self, limit: int = 40) -> list[str]:
        """Titles already written, so the planner does not repeat itself."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT title FROM articles WHERE title <> ''"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [r["title"] for r in rows]

    # -------------------------------------------------------------- articles

    def start_article(self, topic_id: int | None) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO articles (topic_id, created_at, updated_at)"
                " VALUES (?, ?, ?)",
                (topic_id, _now(), _now()),
            )
            return int(cur.lastrowid or 0)

    def save_cost(self, article_id: int, cost: dict[str, Any]) -> None:
        """What this article cost to produce, kept beside the article itself."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE articles SET cost_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(cost, ensure_ascii=False), _now(), article_id),
            )

    def cost_summary(self, limit: int = 30) -> list[dict[str, Any]]:
        """Recent runs and what they cost, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, status, cost_json, created_at FROM articles"
                " WHERE cost_json <> '' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            try:
                cost = json.loads(row["cost_json"])
            except json.JSONDecodeError:
                continue
            out.append({**dict(row), "cost": cost})
        return out

    def save_facts(self, article_id: int, facts: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE articles SET facts_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(facts, ensure_ascii=False), _now(), article_id),
            )

    def finish_article(
        self,
        article_id: int,
        *,
        draft: dict[str, Any],
        status: str,
        verdict: str,
        rounds: int,
        remote_id: str = "",
        error: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE articles SET title = ?, slug = ?, excerpt = ?, body = ?,"
                " status = ?, verdict = ?, rounds = ?, remote_id = ?, error = ?,"
                " updated_at = ? WHERE id = ?",
                (
                    draft.get("title", ""),
                    draft.get("slug", ""),
                    draft.get("excerpt", ""),
                    draft.get("body", ""),
                    status,
                    verdict,
                    rounds,
                    remote_id,
                    error,
                    _now(),
                    article_id,
                ),
            )

    def fail_article(self, article_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE articles SET status = 'failed', error = ?, updated_at = ?"
                " WHERE id = ?",
                (error[:2000], _now(), article_id),
            )

    # --------------------------------------------------------------- reviews

    def save_review(
        self,
        article_id: int,
        round_number: int,
        reviewer: str,
        score: float,
        issues: list[dict[str, Any]],
        summary: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reviews (article_id, round_number, reviewer, score,"
                " issues_json, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    article_id,
                    round_number,
                    reviewer,
                    score,
                    json.dumps(issues, ensure_ascii=False),
                    summary,
                    _now(),
                ),
            )
