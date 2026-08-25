"""The pipeline's own SQLite database.

What to write about, what was written, how it was judged — and what has been
verified. The last of those is the fact registry: a claim that survived the
source audit is kept, with a shelf life, so tomorrow's article can rest on it
without paying to verify it again and, more importantly, so two articles never
quote different numbers for the same thing.

Expiry is the whole discipline. A price is stale in a week, a specification in
months, a standard in years; a registry without shelf lives would not save
work, it would industrialise a stale claim across every article that touched
it. Facts are still archived as JSON on the article row as well, because that
column is the audit trail of what a particular article actually rested on.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .normalize import claim_key

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

-- Claims that survived the source audit, with the date each stops being
-- trustworthy. `claim_key` is the normalised claim, so the same fact arriving
-- in different words or different digits updates the row it already has
-- instead of quietly becoming a second opinion.
CREATE TABLE IF NOT EXISTS facts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_key     TEXT    NOT NULL UNIQUE,
    claim         TEXT    NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'general',
    source        TEXT    NOT NULL DEFAULT '',
    source_url    TEXT    NOT NULL DEFAULT '',
    evidence      TEXT    NOT NULL DEFAULT '',
    confidence    TEXT    NOT NULL DEFAULT 'MEDIUM',
    tier          INTEGER NOT NULL DEFAULT 1,
    tier_label    TEXT    NOT NULL DEFAULT '',
    verified      INTEGER NOT NULL DEFAULT 0,
    audit_note    TEXT    NOT NULL DEFAULT '',
    times_used    INTEGER NOT NULL DEFAULT 0,
    article_id    INTEGER REFERENCES articles(id),
    first_seen    TEXT    NOT NULL,
    verified_at   TEXT    NOT NULL,
    expires_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_article ON reviews(article_id);
CREATE INDEX IF NOT EXISTS idx_categories_status ON categories(status);
CREATE INDEX IF NOT EXISTS idx_facts_expires ON facts(expires_at);
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


def _in_days(days: int) -> str:
    """When something verified now stops being trusted."""
    return (datetime.now(timezone.utc) + timedelta(days=max(days, 0))).isoformat(
        timespec="seconds"
    )


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

    # ------------------------------------------------------------------ facts

    def remember_facts(
        self,
        article_id: int,
        facts: list[dict[str, Any]],
        shelf_life: Callable[[str], int],
    ) -> int:
        """Keep what the audit accepted, with the date it stops being trusted.

        Two kinds of fact are worth remembering: one whose quoted passage was
        actually found on the page it cites, and one resting on a source whose
        authority was never in question — a standards body, a university, the
        manufacturer's own documentation. The second exists because the best
        sources are often the least readable: a datasheet is a PDF, and no
        passage check can look inside it.

        A claim that arrives again in different words updates the row it
        already has. That is the point: the registry holds one answer per
        question, so two articles cannot quote different numbers for the same
        thing.
        """
        stored = 0
        now = _now()
        with self._connect() as conn:
            for fact in facts:
                claim = str(fact.get("claim") or "").strip()
                key = claim_key(claim)
                tier = int(fact.get("tier") or 1)
                verified = bool(fact.get("verified"))
                if not key or not fact.get("allowed", True):
                    continue
                if not verified and tier < 3:
                    continue

                kind = str(fact.get("kind") or "general")
                expires = _in_days(shelf_life(kind))
                conn.execute(
                    "INSERT INTO facts (claim_key, claim, kind, source, source_url,"
                    " evidence, confidence, tier, tier_label, verified, audit_note,"
                    " article_id, first_seen, verified_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(claim_key) DO UPDATE SET"
                    "   claim = excluded.claim,"
                    "   kind = excluded.kind,"
                    "   source = excluded.source,"
                    "   source_url = excluded.source_url,"
                    "   evidence = excluded.evidence,"
                    "   confidence = excluded.confidence,"
                    "   tier = excluded.tier,"
                    "   tier_label = excluded.tier_label,"
                    "   verified = excluded.verified,"
                    "   audit_note = excluded.audit_note,"
                    "   article_id = excluded.article_id,"
                    "   verified_at = excluded.verified_at,"
                    "   expires_at = excluded.expires_at",
                    (
                        key,
                        claim,
                        kind,
                        str(fact.get("source") or ""),
                        str(fact.get("source_url") or ""),
                        str(fact.get("evidence") or ""),
                        str(fact.get("confidence") or "MEDIUM"),
                        tier,
                        str(fact.get("tier_label") or ""),
                        int(verified),
                        str(fact.get("audit_note") or ""),
                        article_id or None,
                        now,
                        now,
                        expires,
                    ),
                )
                stored += 1
        return stored

    def live_facts(self, terms: list[str], limit: int = 20) -> list[dict[str, Any]]:
        """Facts still inside their shelf life that bear on these words.

        Relevance is deliberately crude — a claim matching more of the topic's
        words comes first — because the alternative is an embedding index, and
        a registry that needs one has stopped being auditable.
        """
        wanted = [claim_key(t) for t in terms]
        wanted = [t for t in wanted if len(t) >= 3]
        if not wanted:
            return []

        clause = " OR ".join(["claim_key LIKE ?"] * len(wanted))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM facts WHERE expires_at > ? AND ({clause})"
                " ORDER BY tier DESC, verified DESC, verified_at DESC LIMIT 200",
                (_now(), *[f"%{t}%" for t in wanted]),
            ).fetchall()

        scored = sorted(
            (dict(row) for row in rows),
            key=lambda row: (
                -sum(1 for t in wanted if t in row["claim_key"]),
                -int(row["tier"]),
                -int(row["verified"]),
            ),
        )
        return scored[:limit]

    def mark_facts_used(self, claim_keys: list[str]) -> None:
        if not claim_keys:
            return
        with self._connect() as conn:
            conn.executemany(
                "UPDATE facts SET times_used = times_used + 1 WHERE claim_key = ?",
                [(key,) for key in claim_keys],
            )

    def fact_stats(self) -> dict[str, Any]:
        """What the registry is holding, live and expired, by kind."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, expires_at > ? AS live, COUNT(*) AS n, SUM(times_used)"
                " AS used FROM facts GROUP BY kind, live",
                (_now(),),
            ).fetchall()
        stats: dict[str, Any] = {"live": 0, "expired": 0, "reuses": 0, "by_kind": {}}
        for row in rows:
            bucket = "live" if row["live"] else "expired"
            stats[bucket] += row["n"]
            stats["reuses"] += row["used"] or 0
            stats["by_kind"].setdefault(row["kind"], {"live": 0, "expired": 0})
            stats["by_kind"][row["kind"]][bucket] += row["n"]
        return stats

    def recent_facts(self, limit: int = 30, live_only: bool = True) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM facts"
                + (" WHERE expires_at > ?" if live_only else "")
                + " ORDER BY verified_at DESC LIMIT ?",
                ((_now(), limit) if live_only else (limit,)),
            ).fetchall()
            return [dict(r) for r in rows]

    def forget_fact(self, fact_id: int) -> bool:
        """Drop one remembered claim.

        The escape hatch for a fact that was verified and is still wrong. A
        registry a person cannot correct is one they stop trusting entirely.
        """
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            return cur.rowcount > 0

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
