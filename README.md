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

**The registry is checked against the world.** Sources come from the search's
own grounding metadata rather than from the model's text, are ranked by who
published them, and — the part that closes the loop — the page each fact cites
is read and the passage it quotes is looked for there. A claim whose passage is
not on the page it cites is the one failure no reviewer can catch, because the
article and the registry agree with each other perfectly.

**What has been verified is remembered.** A claim that survives the audit is
stored with a shelf life: days for a price, months for a specification, years
for a standard. Later runs are offered it with a citable id of its own, so
reuse passes the same audit as a fresh search rather than being a special case.
Nothing is reused past its expiry — that is what makes reuse safe, and
`python -m app.cli facts list` is how you see what the pipeline currently
believes.

**The gate is code.** `app/rules.py` decides. Any critical issue blocks. A
citation the registry does not support blocks — and that check runs in Python,
so an invented source cannot survive by sounding convincing. Below the score
bar sends it back. Only when every check passes does a draft ship, and when the
rounds run out it goes to a human instead of shipping anyway.

**What can be counted is not scored.** Most on-page SEO is measurement, not
opinion: title and description lengths, one top-level heading and no skipped
levels, alt text on every picture, a slug that is not already taken, internal
links that resolve to pages the site really has. `app/seo.py` measures those
and the gate enforces them — a collision or a link into a 404 blocks, a length
sends the draft back, polish travels with the decision without costing a round.
The SEO reviewer keeps what is genuinely judgement: whether the article answers
what the searcher asked, and whether it reads well.

**Structured data comes from checked values.** `app/structured_data.py` builds
the JSON-LD in code: `Article` always, `FAQPage` when the writer answered real
questions in its headings, and `Product` filled from the site's own catalogue.
No model writes a field, so a specification cannot reach a rich result by way
of a sentence.

**The judge only runs when needed.** It never approves anything. On a revision
it merges three sets of findings into one ordered list of edits, resolving
contradictions, so the next round fixes named issues instead of rewriting the
article from scratch.

**Articles are a section, not a pile.** The planner sees every keyword an
existing article already took, and a subject whose every query is spoken for is
refused in code — two articles competing for one search split the ranking
between them. Each category can name a pillar article the narrower pieces link
back to (`categories pillar <id> <slug>`), and pages nothing currently links to
are handed to the writer as preferred link targets, so orphans get fixed as a
side effect of writing the next piece.

**Some subjects are never just approved.** An article can pass every check and
still be one nobody should publish unread — a price, a subsidy, a regulation, a
safety claim, or a claim nobody could verify at its source. The gate escalates
those to a person by name instead of approving them, and says why in the
notification. Today every article reaches a person anyway, so this changes a
headline; it exists now because the day automatic publishing arrives is the
wrong day to start deciding which articles it does not apply to.

**Human approval is the last word.** Every article arrives at the site as a
draft. Publishing automatically is a decision for later, once the numbers earn
it.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env      # then fill it in
```

At minimum set `GEMINI_API_KEY`, `IMAGE_API_KEY`, `SITE_API_URL` and
`SITE_API_TOKEN` — two keys because the pictures come from somewhere else; see
**Illustration** below. That file beats the machine's own environment, so a
`GEMINI_API_KEY` exported for some other tool cannot quietly stand in for the
one here. Check what the pipeline sees:

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

### Running it permanently

On a server, with a systemd timer:

```bash
sudo ./deploy/install.sh
```

That installs the code under `/opt`, the database under `/var/lib` where it
survives every update, and the settings under `/etc`; then it enables two
timers — one that writes an article every morning, one that backs the database
up every night. No container: this is two minutes of work a day, and nothing at
all runs between runs. [docs/DEPLOY.md](docs/DEPLOY.md) has the whole runbook —
what the target site must provide first, what to fill in, how to make the first
run publish nothing, and what to do when something breaks.

## What the site must provide

Four endpoints under `SITE_API_URL`, all authenticated with
`Authorization: Bearer $SITE_API_TOKEN`:

| Endpoint | Purpose |
|---|---|
| `POST /posts` | create the draft (`title`, `slug`, `excerpt`, `body`, `status`, `structured_data`, `meta`) |
| `GET /products` | catalogue entries the article may mention — the source of truth for specifications |
| `GET /articles` | what is already published, for internal links and to avoid repeats |
| `GET /stats` | per-article view counts (used later, by the weekly analyst) |

Only `POST /posts` is required. The reads degrade quietly: without
`/products` the product reviewer simply has nothing to check against, and
without `/articles` the gate cannot tell a good internal link from a broken one,
so it stops checking rather than guessing.

`structured_data` arrives as a list of JSON-LD objects. A site that ignores the
field loses nothing; one that renders each object inside a
`<script type="application/ld+json">` tag gets rich results built from values
that were already verified.

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
| `SOURCE_MANUFACTURERS` / `SOURCE_PUBLICATIONS` | who counts as an authority in your subject; without them a datasheet ranks no higher than a blog |
| `SOURCE_VERIFY_EVIDENCE` | read each cited page and look for the passage it was quoted for |
| `FACT_TTL_*_DAYS` | how long a verified claim may be reused before it must be checked again |
| `SEO_TITLE_MAX` / `SEO_DESCRIPTION_MAX` | the lengths the gate counts, in characters |
| `SEO_KNOWN_PATHS` | pages that exist but are neither articles nor products, so a link to them is not read as broken |
| `SITE_PUBLIC_URL` | where readers see the site, for link checking and structured data |
| `IMAGE_PROVIDER` / `IMAGE_MODEL` | who draws the pictures; `openrouter` and Seedream by default, not Gemini |
| `DRY_RUN` | run the whole pipeline without sending anything |

### Illustration

The pictures are the one stage that does not run on Gemini, and provenance is
the reason. Imagen writes **SynthID** into the pixels of everything it makes —
an invisible watermark built to survive cropping, resizing and re-encoding —
so every illustration on the site stays permanently identifiable as generated.
An OpenAI-shaped provider attaches C2PA metadata instead, which the first
resize strips, including the one a CMS does when it builds thumbnails.

Worth being honest about what that buys. Google does not penalise generated
content as such; what a marker earns today is a label beside the picture, not
a lower ranking. The better argument for moving is that the editorial
photography is often simply better elsewhere.

The default is Seedream through OpenRouter, with its own key:

```
IMAGE_PROVIDER=openrouter
IMAGE_API_KEY=...
IMAGE_MODEL=bytedance/seedream-4.5
```

Check the slug against the provider's own model list before trusting it —
image models are renamed and retired faster than anything else here. Setting
`IMAGE_PROVIDER=gemini` puts it back on `GEMINI_API_KEY` and one account.

Nothing here can fail a run: every error in `app/images.py` is logged and
swallowed, because an article without a picture is worth more than no article.
That is also why `check` reports a missing image key — otherwise the only
symptom is articles that quietly stop having pictures.

## Layout

```
app/
  agent.py        the workflow graph — the shape of a run
  rules.py        the gate: the only thing that decides what ships
  seo.py          what can be measured about a draft, measured
  sources.py      where a claim came from, and whether that page says it
  normalize.py    comparing text that was written twice
  structured_data.py  JSON-LD, built from the registry and the catalogue
  schemas.py      the contracts every stage speaks
  prompts.py      agent instructions
  config.py       everything site-specific, read from the environment
  store.py        SQLite: categories, topics, articles, reviews, facts
  site_client.py  the only door to the target site
  notify.py       Telegram, behind a protocol so another channel is one file
  cli.py          check / run / categories / topics / facts / cost / eval
tests/
  unit/           the gate's behaviour, pinned down
  integration/    the graph's shape
```

## Testing

```bash
uv run pytest tests/ --ignore=tests/eval
```

These cover code, not writing quality: the gate's verdicts and the graph's
shape. No model is called, so they run in a second and can be trusted in CI.

Whether the *reviewers* still catch anything is a different question, and it
needs a model:

```bash
uv run python -m app.cli eval --repeat 5
```

That puts drafts with known defects in front of the real review board and
reports three numbers — how many defects were caught, how often a clean draft
was wrongly failed, and the gap between what a clean draft scores and what a
defective one scores. The last one is the calibration number: a board that
scores everything alike is applauding, not reviewing. Runs are saved and each
one prints what moved since the last. See [tests/eval/](tests/eval/) for what
the scenarios are and what the whole-pipeline dataset costs.

## Status

This is the first working version, and it works end to end: a queued topic
produces a sourced, reviewed, human-approvable draft on a real site.

The full architecture was designed before any of it was built, then cut down to
a first version small enough to finish. What was left out — a persistent fact
registry with expiry, a separate source validator, five specialised reviewers
instead of three, automatic publishing, and a weekly analyst that scores topics
by how they actually performed — is in [ROADMAP.md](ROADMAP.md), along with
what the first live runs revealed.

## License

Apache 2.0.
