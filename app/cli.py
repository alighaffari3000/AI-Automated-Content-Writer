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
from .agent import agent_models, get_store
from .config import settings, unedited_settings
from .cost import RunCost, usage_from_event
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
    cost = RunCost()
    models = agent_models()
    last: str = ""
    async for event in runner.run_async(
        user_id="scheduler",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=TRIGGER)]),
    ):
        usage = usage_from_event(event, models)
        if usage:
            cost.record_tokens(*usage)
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    last = part.text

    # Pictures are made inside the graph rather than through the runner, so
    # their count comes back through state.
    final = await runner.session_service.get_session(
        app_name=adk_app.name, user_id="scheduler", session_id=session.id
    )
    state = getattr(final, "state", {}) or {}
    for _ in range(int(state.get("images_generated", 0))):
        cost.record_image()

    rates, image_usd = settings.cost.rates, settings.cost.image_usd
    logger.info("Run cost:\n%s", cost.summary(rates, image_usd))

    article_id = int(state.get("article_id", 0))
    if article_id:
        get_store().save_cost(article_id, cost.to_dict(rates, image_usd))

    if last:
        print(last)
        notifier = build_notifier(settings.notify)
        notifier.send(
            f"💰 {cost.total_calls} calls · "
            f"{cost.total_input + cost.total_output:,} tokens · "
            f"~${cost.estimate_usd(rates, image_usd):.3f}"
            + (f" · {cost.images} image(s)" if cost.images else "")
        )
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
        (
            "measured",
            f"title <= {settings.seo.title_max}, "
            f"description {settings.seo.description_min}-{settings.seo.description_max}, "
            f"faq from {settings.seo.min_faq_entries} question(s)",
        ),
        (
            "structured data",
            "on" if settings.seo.structured_data else "off",
        ),
        (
            "sources",
            f"{len(settings.sources.manufacturers)} manufacturer(s), "
            f"{len(settings.sources.publications)} publication(s) configured; "
            + ("passage checked" if settings.sources.verify_evidence else "passage unchecked"),
        ),
        (
            "registry",
            (
                f"on, {settings.registry.ttl_days.get('specification')}d for a "
                f"specification, {settings.registry.ttl_days.get('price')}d for a price"
                if settings.registry.enabled
                else "off"
            ),
        ),
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
    unedited = unedited_settings()
    if unedited:
        # Worth naming before anything else: every other complaint below is a
        # consequence of this one, and the file looks filled in.
        problems.append(
            "Still holding the example file's placeholder value, so they count "
            "as unset: " + ", ".join(unedited)
        )
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
        if row["pillar_slug"]:
            print(f"       pillar: /{row['pillar_slug']}")
    return 0


def cmd_categories_pillar(args: argparse.Namespace) -> int:
    """Name the article a cluster hangs off.

    Every narrower piece written in this area is then asked to link back to it,
    which is how a pile of articles becomes a section of a site.
    """
    if not get_store().set_pillar(args.id, args.slug):
        print(f"No category with id {args.id}.")
        return 1
    print(f"category #{args.id} now hangs off /{args.slug.strip().lower()}")
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


def cmd_eval(args: argparse.Namespace) -> int:
    """Measure whether the reviewers still catch planted defects."""
    from . import evaluation

    failures = evaluation.main(repeat=args.repeat, keep=not args.no_save)
    return 1 if failures else 0


def cmd_cost(args: argparse.Namespace) -> int:
    """What runs have actually been costing."""
    runs = get_store().cost_summary(args.limit)
    if not runs:
        print("No costed runs yet.")
        return 0

    total = sum(r["cost"].get("estimated_usd", 0) for r in runs)
    tokens = sum(
        r["cost"].get("input_tokens", 0) + r["cost"].get("output_tokens", 0) for r in runs
    )
    print(f"{len(runs)} run(s), {tokens:,} tokens, ~${total:.2f} total\n")
    for run in runs:
        c = run["cost"]
        print(
            f"#{run['id']:<4} {(run['created_at'] or '')[:10]}  "
            f"${c.get('estimated_usd', 0):.4f}  "
            f"{c.get('input_tokens', 0):>7,} in / {c.get('output_tokens', 0):>6,} out  "
            f"{c.get('images', 0)} img  {run['status']}"
        )
        if run["title"]:
            print(f"      {run['title'][:78]}")
    print(f"\naverage per run: ${total / len(runs):.4f}")
    print(f"at one a day, that is about ${total / len(runs) * 30:.2f} a month.")
    return 0


def cmd_facts_list(args: argparse.Namespace) -> int:
    """What the pipeline currently believes, and until when."""
    store = get_store()
    stats = store.fact_stats()
    rows = store.recent_facts(args.limit, live_only=not args.all)

    print(
        f"{stats['live']} live fact(s), {stats['expired']} expired, "
        f"reused {stats['reuses']} time(s)."
    )
    for kind, counts in sorted(stats["by_kind"].items()):
        print(f"  {kind:<15} {counts['live']:>4} live  {counts['expired']:>4} expired")
    if not rows:
        print("\nNothing stored yet — facts are remembered as articles are written.")
        return 0

    print()
    for row in rows:
        state = "verified" if row["verified"] else "on authority"
        print(
            f"#{row['id']:<4} {row['kind']:<14} {row['confidence']:<7} {state:<12}"
            f" until {row['expires_at'][:10]}  used {row['times_used']}x"
        )
        print(f"      {row['claim'][:96]}")
        if args.verbose and row["source_url"]:
            print(f"      ↳ {row['source_url']}")
    return 0


def cmd_facts_forget(args: argparse.Namespace) -> int:
    """Drop a claim that is stored and wrong."""
    if get_store().forget_fact(args.id):
        print(f"fact #{args.id} forgotten.")
        return 0
    print(f"No fact with id {args.id}.")
    return 1


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

    pillar = cats_sub.add_parser(
        "pillar",
        help="name the article this area's pieces link back to",
        parents=[common],
    )
    pillar.add_argument("id", type=int)
    pillar.add_argument("slug", help="slug of an article already published on the site")
    pillar.set_defaults(func=cmd_categories_pillar)

    pause = cats_sub.add_parser(
        "pause", help="take an area out of rotation (never deleted)", parents=[common]
    )
    pause.add_argument("id", type=int)
    pause.add_argument("--resume", action="store_true", help="put it back in rotation")
    pause.set_defaults(func=cmd_categories_pause)

    evaluate = sub.add_parser(
        "eval",
        help="check that the reviewers still catch planted defects",
        parents=[common],
    )
    evaluate.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run every scenario N times; model output varies, so a number "
        "worth comparing needs more than one sample",
    )
    evaluate.add_argument(
        "--no-save", action="store_true", help="do not keep this run for comparison"
    )
    evaluate.set_defaults(func=cmd_eval)

    cost = sub.add_parser("cost", help="what runs have been costing", parents=[common])
    cost.add_argument("--limit", type=int, default=30)
    cost.set_defaults(func=cmd_cost)

    facts = sub.add_parser("facts", help="what the pipeline has verified and remembers")
    facts_sub = facts.add_subparsers(dest="facts_command", required=True)
    facts_list = facts_sub.add_parser(
        "list", help="stored claims and their shelf life", parents=[common]
    )
    facts_list.add_argument("--limit", type=int, default=30)
    facts_list.add_argument(
        "--all", action="store_true", help="include claims past their shelf life"
    )
    facts_list.set_defaults(func=cmd_facts_list)

    forget = facts_sub.add_parser(
        "forget", help="drop a stored claim that turned out to be wrong", parents=[common]
    )
    forget.add_argument("id", type=int)
    forget.set_defaults(func=cmd_facts_forget)

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
