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
CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL UNIQUE,
    notes         TEXT    NOT NULL DEFAULT '',
    keywords      TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'active',
    priority      INTEGER NOT NULL DEFAULT 0,
    score         REAL    NOT NULL DEFAULT 0,
    times_used    INTEGER NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring a database made by an earlier version up to date."""
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(topics)")}
        if "times_used" not in columns:
            conn.execute(
                "ALTER TABLE topics ADD COLUMN times_used INTEGER NOT NULL DEFAULT 0"
            )
            # Topics used to be consumed once and marked 'used'. They now cycle,
            # so that state becomes "written once already, go to the back".
            conn.execute(
                "UPDATE topics SET times_used = 1 WHERE status = 'used'"
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

    # ---------------------------------------------------------------- topics

    def add_topic(
        self,
        title: str,
        notes: str = "",
        keywords: str = "",
        priority: int = 0,
    ) -> int:
        """Add a topic to the queue. Re-adding an existing title is a no-op."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO topics (title, notes, keywords, priority, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (title.strip(), notes.strip(), keywords.strip(), priority, _now()),
            )
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = conn.execute(
                "SELECT id FROM topics WHERE title = ?", (title.strip(),)
            ).fetchone()
            return int(row["id"]) if row else 0

    def next_topic(self) -> dict[str, Any] | None:
        """The topic to write today.

        The bank is permanent: a topic is never consumed, it just goes to the
        back of the queue. Fewest-times-written comes first, so every topic is
        covered once before any is covered twice — a rule that holds however
        close together the runs happen, which ordering by timestamp alone does
        not. Then oldest-first, then priority, then score.

        A topic switched off by hand (`status = 'paused'`) is skipped without
        being removed.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM topics WHERE status <> 'paused'"
                " ORDER BY times_used ASC, used_at IS NOT NULL, used_at ASC,"
                "          priority DESC, score DESC, id ASC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def mark_topic_used(self, topic_id: int) -> None:
        """Send a topic to the back of the queue. It is never removed."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE topics SET used_at = ?, times_used = times_used + 1"
                " WHERE id = ?",
                (_now(), topic_id),
            )

    def release_topic(self, topic_id: int) -> None:
        """Undo a claim after a failed run, so the topic comes up again next."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE topics SET used_at = NULL,"
                " times_used = MAX(times_used - 1, 0) WHERE id = ?",
                (topic_id,),
            )

    def articles_for_topic(self, topic_id: int, limit: int = 10) -> list[dict[str, Any]]:
        """What this pipeline already published on this topic.

        The bank cycles, so a topic comes back around. The writer needs to know
        what was said last time — otherwise the second article on a subject is
        the first one again, in different words.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT title, excerpt, created_at FROM articles"
                " WHERE topic_id = ? AND title <> '' AND status = 'draft_sent'"
                " ORDER BY id DESC LIMIT ?",
                (topic_id, limit),
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
