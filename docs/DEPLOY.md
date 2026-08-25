# Running this permanently

The pipeline is a daily job, not a service. It wakes up, writes one article,
files it as a draft and exits. So there is nothing to keep running and nothing
listening on a port — what has to be permanent is the schedule, the database,
and the secrets.

These instructions put it on a Linux server with a systemd timer. No container:
this is two minutes of work a day, and a container runtime would be a daemon
running all day to host it. The dependencies are prebuilt wheels — about 280 MB,
downloaded once, no compiler involved.

It ends up as three directories, each with one job:

| Directory | Holds | On update |
|---|---|---|
| `/opt/content-writer` | the code and its virtualenv | replaced |
| `/var/lib/content-writer` | the database and its backups | never touched |
| `/etc/content-writer` | the keys and the settings | never touched |

## Before you start

**The target site must answer four endpoints.** Nothing else here matters until
it does. The pipeline talks to one HTTP API, documented in the main
[README](../README.md#what-the-site-must-provide):

| Endpoint | Needed for |
|---|---|
| `POST /posts` | filing the draft — required |
| `POST /media` | uploading the generated images |
| `GET /products` | the catalogue the product reviewer checks against |
| `GET /articles` | internal links, orphan detection, avoiding repeats |
| `GET /taxonomy` | the categories and tags the writer may use |

All of them authenticate with `Authorization: Bearer $SITE_API_TOKEN`. Only
`POST /posts` is required; the reads degrade quietly, but each one you skip
switches off a check — without `/articles`, for instance, the gate cannot tell a
good internal link from a broken one, so it stops checking rather than guessing.

**And the server needs:**

- systemd, and `curl` for the one-time uv install
- its clock set to the timezone you want the articles written in
  (`timedatectl set-timezone Asia/Tehran`)
- outbound HTTPS to the model API and to whatever the research reads
- about 400 MB of disk, and very little else — a run is a couple of minutes of
  one core, and nothing at all runs between runs

No system Python is needed: uv fetches its own if the server's is too old.

## Install

```bash
git clone https://github.com/alighaffari3000/AI-Automated-Content-Writer.git
cd AI-Automated-Content-Writer
sudo ./deploy/install.sh
```

That creates a `contentwriter` system user, installs uv if it is missing, copies
the code to `/opt/content-writer` and builds its environment there, creates
`/var/lib/content-writer` for the database, copies `.env.example` to
`/etc/content-writer/env` — with `DRY_RUN=true`, so a fresh install cannot
publish to a live site by accident — and enables two timers: one that writes an
article every morning, one that backs up the database every night.

It is safe to run again: it never touches the database or an environment file
that already exists.

## Fill in the secrets

Edit `/etc/content-writer/env`. It is read by python-dotenv, so it takes the
same syntax as `.env.example`, comments and all. The file ends up
`root:contentwriter` mode `640` — it holds your API keys, so it is readable by
the service and by nobody else. If you replace it by hand, keep those
permissions or the service starts with no configuration at all.

At minimum:

```ini
GEMINI_API_KEY=...
SITE_API_URL=https://yoursite.example/api/automation
SITE_API_TOKEN=...
```

The file arrives as a copy of the example, so every one of those lines already
has something on it. The placeholders are not treated as configuration: a
setting still reading `https://example.com/api/automation` or
`your-api-key-here` counts as unset, `cw check` shows it as `NOT SET` and names
it, and the pipeline will not file an article with a site nobody chose. If the
site's API does not exist yet, leaving them as they came is fine — that is the
same as leaving them empty.

Worth setting before the first real run:

| Setting | Why |
|---|---|
| `CONTENT_DOMAIN`, `CONTENT_AUDIENCE`, `CONTENT_TONE` | what the site is about and who reads it |
| `SOURCE_MANUFACTURERS`, `SOURCE_PUBLICATIONS` | who counts as an authority in your subject — without them a manufacturer's datasheet ranks no higher than a blog |
| `SAFETY_TERMS` | the words, in your language, that send an article to a person regardless of its score |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | how you find out a draft is waiting, or that a run failed |
| `DRY_RUN` | left `true` by the installer: everything happens except sending anything anywhere |

`DB_PATH` is set by the service unit and does not belong in this file.

## First run

A shorthand for talking to the pipeline as the service does, worth putting in
your shell profile:

```bash
alias cw='sudo -u contentwriter env HOME=/opt/content-writer \
    DB_PATH=/var/lib/content-writer/pipeline.db \
    /usr/local/bin/uv run --no-sync --project /opt/content-writer python -m app.cli'
```

Then:

```bash
cw check                 # what is configured, and what is missing
cw categories seed       # or `categories add "..."` for your own subject areas
```

Still in dry run, write one article for real:

```bash
sudo systemctl start content-writer.service
journalctl -u content-writer.service -n 200 --no-pager
```

Read what it produced before letting it publish anything:

```bash
cw topics list
```

When you are satisfied, remove `DRY_RUN` from the environment file. From then on
every morning's article arrives on the site as a draft awaiting your approval —
that part does not change, whatever the gate decided.

## Living with it

```bash
systemctl list-timers 'content-writer*'      # when it next runs
journalctl -u content-writer.service -f      # watch a run
cw facts list                                # what it has verified and remembers
cw cost --limit 30                           # what it has been costing
cw categories list                           # rotation order, and each cluster's pillar
cw eval --repeat 5                           # are the reviewers still catching things
```

Change the hour with `sudo systemctl edit content-writer.timer`, or by editing
`/etc/systemd/system/content-writer.timer` and running `systemctl daemon-reload`.

To pause it: `sudo systemctl disable --now content-writer.timer`. Nothing is
lost; the database stays.

## Updating

```bash
cd AI-Automated-Content-Writer
git pull
sudo ./deploy/install.sh
```

The code in `/opt/content-writer` is replaced and its environment rebuilt; the
database and the settings are left alone. The database migrates itself on the
next run: new columns and tables are added in place, and no existing row is
dropped.

## Backups and restore

`content-writer-backup.timer` snapshots the database nightly into
`/var/lib/content-writer/backups/`, keeping the newest fourteen. It uses
SQLite's own backup API rather than copying the file, because a copy taken
mid-write restores as a corrupt database.

This is the one thing here that cannot be rebuilt: the fact registry with its
shelf lives, which subjects are covered, what each run cost. Copy those
snapshots off the machine as well — a backup on the same disk is not a backup.

To restore, put the file back and let it be found:

```bash
sudo systemctl stop content-writer.timer
sudo cp /var/lib/content-writer/backups/pipeline-YYYYMMDDTHHMMSSZ.db \
        /var/lib/content-writer/pipeline.db
sudo chown contentwriter:contentwriter /var/lib/content-writer/pipeline.db
sudo systemctl start content-writer.timer
```

## When something goes wrong

The pipeline tells you first: a failed run sends a Telegram message with the
reason, if Telegram is configured. Then `journalctl -u content-writer.service`
has the whole run.

| What you see | What it usually is |
|---|---|
| `SITE_API_URL / SITE_API_TOKEN are required` | the environment file is not filled in, or the symlink at `/opt/content-writer/.env` is gone |
| `Still holding the example file's placeholder value` | `/etc/content-writer/env` is a copy of `.env.example` with those lines unedited |
| A 404 from the model | on a `GEMINI_API_KEY`, a retired model name — check `MODEL_AUTHOR` and `MODEL_WORKER`. On Vertex (`GOOGLE_GENAI_USE_VERTEXAI=true`) it is usually the region instead: set `GOOGLE_CLOUD_LOCATION=global` |
| `Research found no source solid enough to write from` | the run stopped on purpose; nothing was published |
| Every fact ranks as "general web" | `SOURCE_MANUFACTURERS` is empty |
| Permission denied writing the database | `sudo chown -R contentwriter:contentwriter /var/lib/content-writer` |
| `uv: command not found` from systemd | uv is not at `/usr/local/bin/uv`; re-run the installer or edit the unit's `ExecStart` |
| A run stuck for hours | `sudo systemctl stop content-writer.service`; the next run recovers the abandoned article by itself |

A run that dies halfway leaves nothing broken behind: the next one rolls back
the abandoned article, gives the category its turn back, and un-logs the
subject so the planner may propose it again.
