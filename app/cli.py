"""Command line entry points.

    python -m app.cli check              show what is configured and what is missing
    python -m app.cli topics add "..."   queue a topic
    python -m app.cli topics list        show the queue
    python -m app.cli run                produce today's draft (this is the cron job)

`run` is deliberately quiet on success and loud on failure: it is meant to be
called by cron and read in a log afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

from google.adk.runners import InMemoryRunner
from google.genai import types

from .agent import app as adk_app
from .agent import get_store
from .config import settings
from .notify import build_notifier

logger = logging.getLogger("content_writer")

TRIGGER = "Produce today's article."


def _setup_logging(verbose: bool) -> None:
    # The articles are not in Latin script and Windows consoles default to a
    # codepage that cannot encode them, which turns a log line into a crash
    # inside the logging handler.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _run_once() -> int:
    runner = InMemoryRunner(app=adk_app)
    session = await runner.session_service.create_session(
        app_name=adk_app.name, user_id="scheduler", session_id=f"run-{uuid.uuid4().hex[:12]}"
    )
    last: str = ""
    async for event in runner.run_async(
        user_id="scheduler",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=TRIGGER)]),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    last = part.text
    if last:
        print(last)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_run_once())
    except Exception as exc:  # noqa: BLE001 - cron needs the reason, not a traceback
        logger.exception("The run failed.")
        build_notifier(settings.notify).send(f"Today's article run failed: {exc}")
        return 1


def cmd_check(args: argparse.Namespace) -> int:
    """Print the effective configuration so a misconfigured cron is obvious."""
    site = settings.site
    notify = settings.notify
    rows = [
        ("author model", settings.models.author),
        ("worker model", settings.models.worker),
        ("language", f"{settings.content.language_name} ({settings.content.language})"),
        ("domain", settings.content.domain),
        ("site API", site.api_url or "NOT SET"),
        ("site token", "set" if site.api_token else "NOT SET"),
        ("telegram", "configured" if notify.telegram_configured else "not configured"),
        ("database", settings.db_path),
        ("max rounds", str(settings.quality.max_revision_rounds)),
        ("score bar", f"avg >= {settings.quality.min_average_score}, seo >= {settings.quality.min_seo_score}"),
        ("dry run", "yes" if settings.dry_run else "no"),
    ]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"{key.rjust(width)} : {value}")

    store = get_store()
    topic = store.next_topic()
    print(f"{'next topic'.rjust(width)} : {topic['title'] if topic else 'QUEUE EMPTY'}")

    problems = []
    if not site.configured:
        problems.append("SITE_API_URL / SITE_API_TOKEN are required to deliver drafts.")
    if not topic:
        problems.append("The topic queue is empty; add topics before the next run.")
    for problem in problems:
        print(f"  ! {problem}")
    return 1 if problems else 0


def cmd_topics_add(args: argparse.Namespace) -> int:
    topic_id = get_store().add_topic(
        title=args.title,
        notes=args.notes or "",
        keywords=args.keywords or "",
        priority=args.priority,
    )
    print(f"queued #{topic_id}: {args.title}")
    return 0


def cmd_topics_list(args: argparse.Namespace) -> int:
    """The bank, in the order it will be written — next one first."""
    store = get_store()
    with store._connect() as conn:  # noqa: SLF001 - a CLI reading its own store
        rows = conn.execute(
            "SELECT id, title, status, priority, score, times_used, used_at"
            " FROM topics"
            " ORDER BY status = 'paused', times_used ASC,"
            "          used_at IS NOT NULL, used_at ASC,"
            "          priority DESC, score DESC, id ASC"
        ).fetchall()
    if not rows:
        print('No topics yet. Add one with: python -m app.cli topics add "..."')
        return 0

    print(f"{len(rows)} topic(s) in the bank, next to be written first:\n")
    for position, row in enumerate(rows, start=1):
        marker = "  " if row["status"] == "paused" else ("->" if position == 1 else "  ")
        last = (row["used_at"] or "never")[:10]
        print(
            f"{marker} #{row['id']:<4} {row['status']:<7} p{row['priority']:<3} "
            f"written {row['times_used']:<3} last {last:<11} {row['title']}"
        )
    return 0


def cmd_topics_pause(args: argparse.Namespace) -> int:
    """Take a topic out of rotation without deleting it."""
    status = "active" if args.resume else "paused"
    store = get_store()
    with store._connect() as conn:  # noqa: SLF001 - a CLI writing its own store
        changed = conn.execute(
            "UPDATE topics SET status = ? WHERE id = ?", (status, args.id)
        ).rowcount
    if not changed:
        print(f"No topic with id {args.id}.")
        return 1
    print(f"topic #{args.id} is now {status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # -v works on either side of the subcommand; typing it after is the reflex.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(
        prog="app.cli", description=__doc__, parents=[common]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="produce today's draft", parents=[common]).set_defaults(
        func=cmd_run
    )
    sub.add_parser(
        "check", help="show configuration and readiness", parents=[common]
    ).set_defaults(func=cmd_check)

    topics = sub.add_parser("topics", help="manage the topic queue")
    topics_sub = topics.add_subparsers(dest="topics_command", required=True)

    add = topics_sub.add_parser("add", help="queue a topic", parents=[common])
    add.add_argument("title")
    add.add_argument("--notes", default="", help="angle, constraints, anything useful")
    add.add_argument("--keywords", default="", help="comma-separated target keywords")
    add.add_argument("--priority", type=int, default=0, help="higher runs sooner")
    add.set_defaults(func=cmd_topics_add)

    topics_sub.add_parser(
        "list", help="show the bank in writing order", parents=[common]
    ).set_defaults(func=cmd_topics_list)

    pause = topics_sub.add_parser(
        "pause", help="take a topic out of rotation (it is never deleted)",
        parents=[common],
    )
    pause.add_argument("id", type=int)
    pause.add_argument("--resume", action="store_true", help="put it back in rotation")
    pause.set_defaults(func=cmd_topics_pause)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
