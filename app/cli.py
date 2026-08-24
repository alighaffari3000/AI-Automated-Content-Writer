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
        # A previous run that was killed mid-flight left its article stuck, its
        # category's turn consumed and its topic falsely logged as covered.
        # Recover before starting, so one crash never distorts the rotation.
        recovered = get_store().recover_abandoned_runs()
        if recovered:
            logger.warning("Recovered %s abandoned run(s) from a previous crash.", recovered)
        return asyncio.run(_run_once())
    except KeyboardInterrupt:
        # Ctrl-C is how a person abandons a run on purpose. It is not an
        # Exception, so without this clause it would skip every cleanup path
        # and leave the article stuck, the category's turn consumed and the
        # subject falsely logged. Zero hours is safe here: this process's run
        # is the only one young enough to match.
        recovered = get_store().recover_abandoned_runs(older_than_hours=0)
        logger.warning("Run interrupted; rolled back %s run(s).", recovered)
        return 130
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
    category = store.next_category()
    print(
        f"{'next category'.rjust(width)} : "
        f"{category['name'] if category else 'NONE DEFINED'}"
    )
    print(f"{'images'.rjust(width)} : {settings.images.model if settings.images.enabled else 'off'}")

    problems = []
    if not site.configured:
        problems.append("SITE_API_URL / SITE_API_TOKEN are required to deliver drafts.")
    if not category:
        problems.append(
            "No subject areas defined; run `python -m app.cli categories seed`."
        )
    for problem in problems:
        print(f"  ! {problem}")
    return 1 if problems else 0


def cmd_categories_add(args: argparse.Namespace) -> int:
    category_id = get_store().add_category(
        name=args.name, description=args.description or "", audience=args.audience or ""
    )
    print(f"category #{category_id}: {args.name}")
    return 0


def cmd_categories_seed(args: argparse.Namespace) -> int:
    added = get_store().seed_default_categories()
    print(f"{added} default categor{'y' if added == 1 else 'ies'} added.")
    return cmd_categories_list(args)


def cmd_categories_list(args: argparse.Namespace) -> int:
    """Subject areas, in the order they will be written — next one first."""
    store = get_store()
    with store._connect() as conn:  # noqa: SLF001 - a CLI reading its own store
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM topics t WHERE t.category_id = c.id)"
            "        AS topic_count"
            " FROM categories c"
            " ORDER BY c.status = 'paused', c.times_used ASC,"
            "          c.used_at IS NOT NULL, c.used_at ASC, c.id ASC"
        ).fetchall()
    if not rows:
        print(
            "No categories yet. Start with the defaults:\n"
            "  python -m app.cli categories seed\n"
            'Or define your own: python -m app.cli categories add "..." --description "..."'
        )
        return 0

    print(f"{len(rows)} subject area(s), next up first:\n")
    for position, row in enumerate(rows, start=1):
        marker = "  " if row["status"] == "paused" else ("->" if position == 1 else "  ")
        last = (row["used_at"] or "never")[:10]
        print(
            f"{marker} #{row['id']:<3} {row['status']:<7} written {row['times_used']:<3} "
            f"last {last:<11} {row['name']}"
        )
        if row["description"]:
            print(f"       {row['description'][:88]}")
    return 0


def cmd_categories_pause(args: argparse.Namespace) -> int:
    """Take a subject area out of rotation without deleting it."""
    status = "active" if args.resume else "paused"
    with get_store()._connect() as conn:  # noqa: SLF001
        changed = conn.execute(
            "UPDATE categories SET status = ? WHERE id = ?", (status, args.id)
        ).rowcount
    if not changed:
        print(f"No category with id {args.id}.")
        return 1
    print(f"category #{args.id} is now {status}")
    return 0


def cmd_topics_list(args: argparse.Namespace) -> int:
    """What the planner has already invented, newest first."""
    with get_store()._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT t.id, t.title, t.angle, t.created_at, c.name AS category"
            " FROM topics t LEFT JOIN categories c ON c.id = t.category_id"
            " ORDER BY t.id DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    if not rows:
        print("Nothing written yet — the planner invents a subject on each run.")
        return 0
    print(f"{len(rows)} subject(s) covered so far, newest first:\n")
    for row in rows:
        print(f"#{row['id']:<4} {(row['created_at'] or '')[:10]}  [{row['category'] or '-'}]")
        print(f"      {row['title']}")
        if row["angle"]:
            print(f"      ↳ {row['angle'][:92]}")
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

    cats = sub.add_parser("categories", help="the subject areas you maintain")
    cats_sub = cats.add_subparsers(dest="categories_command", required=True)

    add = cats_sub.add_parser("add", help="define a subject area", parents=[common])
    add.add_argument("name")
    add.add_argument(
        "--description", default="", help="what this area covers, in a sentence or two"
    )
    add.add_argument("--audience", default="", help="who reads articles in this area")
    add.set_defaults(func=cmd_categories_add)

    cats_sub.add_parser(
        "seed", help="add a starting set of subject areas", parents=[common]
    ).set_defaults(func=cmd_categories_seed)

    cats_sub.add_parser(
        "list", help="show subject areas in rotation order", parents=[common]
    ).set_defaults(func=cmd_categories_list)

    pause = cats_sub.add_parser(
        "pause", help="take an area out of rotation (never deleted)", parents=[common]
    )
    pause.add_argument("id", type=int)
    pause.add_argument("--resume", action="store_true", help="put it back in rotation")
    pause.set_defaults(func=cmd_categories_pause)

    topics = sub.add_parser("topics", help="what the planner has written about")
    topics_sub = topics.add_subparsers(dest="topics_command", required=True)
    history = topics_sub.add_parser(
        "list", help="subjects covered so far", parents=[common]
    )
    history.add_argument("--limit", type=int, default=40)
    history.set_defaults(func=cmd_topics_list)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
