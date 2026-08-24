# AI Automated Content Writer

A multi-agent pipeline that researches, writes, reviews and revises one article
per day, then hands it to a human as a draft. Built on the
[Google Agent Development Kit](https://adk.dev/) (ADK 2.x).

It is not a "write me a blog post" script. Everything here exists to stop a
plausible-sounding wrong article from reaching a real website: facts are
registered with their sources before a word is written, three reviewers examine
the draft independently, and the decision to ship is made by plain code rather
than by a language model.

The pipeline is site-agnostic. It talks to one small HTTP API, and which site
that is comes from configuration.

## How a run works

```
        cron (once a day)
              │
              ▼
      load context ──────────► no topic queued ──► stop and say so
              │
              ▼
        researcher            one agent, web search, gathers claims + sources
              │
              ▼
       fact registry ────────► nothing solid enough ──► stop and say so
              │               (per run; verifiable claims only)
              ▼
          writer ◄──────────────────────┐
              │                         │
              ▼                         │
   ┌──────────────────────┐             │
   │  three reviewers,    │             │
   │  in parallel, blind  │             │
   │  to each other       │             │
   │                      │             │
   │  technical / facts   │             │
   │  product accuracy    │             │
   │  SEO + editorial     │             │
   └──────────┬───────────┘             │
              ▼                         │
      evaluate_reviews()                │  plain Python, no model
       │              │                 │
   approve         revise ──► judge ────┘  ordered fix list, max 3 rounds
       │
       ▼
   draft to the site  ──►  human approves in the site's own admin panel
       │
       ▼
   Telegram notification
```

### The parts that matter

**The fact registry.** The researcher gathers claims; a second pass turns them
into structured records — claim, source, URL, the supporting passage, a
confidence level, and whether the claim is usable at all. The writer may only
state something checkable if a usable record backs it. General explanatory
sentences need no record; registering truisms would only bury the real claims.

**The gate is code.** `app/rules.py` decides. Any critical issue blocks. A
citation the registry does not support blocks — and that check runs in Python,
so an invented source cannot survive by sounding convincing. Below the score
bar sends it back. Only when every check passes does a draft ship, and when the
rounds run out it goes to a human instead of shipping anyway.

**The judge only runs when needed.** It never approves anything. On a revision
it merges three sets of findings into one ordered list of edits, resolving
contradictions, so the next round fixes named issues instead of rewriting the
article from scratch.

**Human approval is the last word.** Every article arrives at the site as a
draft. Publishing automatically is a decision for later, once the numbers earn
it.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env      # then fill it in
```

At minimum set `GEMINI_API_KEY`, `SITE_API_URL` and `SITE_API_TOKEN`. Check
what the pipeline sees:

```bash
uv run python -m app.cli check
```

Queue something to write about, then produce a draft:

```bash
uv run python -m app.cli topics add "How to size a home battery" --keywords "battery sizing,home storage"
uv run python -m app.cli run
```

Set `DRY_RUN=true` for the first runs — the pipeline does everything except
send the article anywhere.

### Daily schedule

```
0 6 * * *  cd /srv/content-writer && /usr/local/bin/uv run python -m app.cli run >> logs/run.log 2>&1
```

## What the site must provide

Four endpoints under `SITE_API_URL`, all authenticated with
`Authorization: Bearer $SITE_API_TOKEN`:

| Endpoint | Purpose |
|---|---|
| `POST /posts` | create the draft (`title`, `slug`, `excerpt`, `body`, `status`, `meta`) |
| `GET /products` | catalogue entries the article may mention — the source of truth for specifications |
| `GET /articles` | what is already published, for internal links and to avoid repeats |
| `GET /stats` | per-article view counts (used later, by the weekly analyst) |

Only `POST /posts` is required. The reads degrade quietly: without
`/products` the product reviewer simply has nothing to check against.

## Configuration

Everything lives in `.env`; see `.env.example` for the full list with comments.
The settings worth knowing:

| Variable | Meaning |
|---|---|
| `MODEL_AUTHOR` / `MODEL_WORKER` | the writer and judge run on the stronger model; researcher and reviewers on the cheaper one |
| `CONTENT_LANGUAGE_NAME` | the language the article is written in |
| `CONTENT_DOMAIN` / `CONTENT_AUDIENCE` / `CONTENT_TONE` | what the site is about and who reads it |
| `MAX_REVISION_ROUNDS` | how many times a draft may be sent back (default 3) |
| `MIN_AVERAGE_SCORE` / `MIN_SEO_SCORE` | the bars the gate enforces |
| `DRY_RUN` | run the whole pipeline without sending anything |

## Layout

```
app/
  agent.py        the workflow graph — the shape of a run
  rules.py        the gate: the only thing that decides what ships
  schemas.py      the contracts every stage speaks
  prompts.py      agent instructions
  config.py       everything site-specific, read from the environment
  store.py        SQLite: topics, articles, reviews
  site_client.py  the only door to the target site
  notify.py       Telegram, behind a protocol so another channel is one file
  cli.py          check / topics / run
tests/
  unit/           the gate's behaviour, pinned down
  integration/    the graph's shape
```

## Testing

```bash
uv run pytest tests/ --ignore=tests/eval
```

These cover code, not writing quality: the gate's verdicts and the graph's
shape. Whether the agents write well is a question for evaluation
(`tests/eval/`), which is not part of this first version.

## Status

This is the first working version. Deliberately left for later: a persistent
fact registry with expiry, a separate source validator, five specialised
reviewers instead of three, automatic publishing, and the weekly analyst that
scores topics by how they actually performed.

## License

Apache 2.0.
