#!/bin/sh
# Copy the pipeline database somewhere safe, correctly.
#
# `sqlite3.backup()` rather than `cp`: a plain copy taken while the daily run
# happens to be writing produces a file that looks fine and restores as a
# corrupt database. The backup API takes a consistent snapshot of a live file.
#
# Installed to /usr/local/lib/content-writer/backup.sh by deploy/install.sh and
# run by content-writer-backup.timer.

set -eu

DATA_DIR="${DATA_DIR:-/var/lib/content-writer}"
PYTHON="${PYTHON:-/opt/content-writer/.venv/bin/python}"
KEEP="${KEEP:-14}"

if [ ! -f "$DATA_DIR/pipeline.db" ]; then
    echo "No database at $DATA_DIR/pipeline.db yet; nothing to back up."
    exit 0
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$DATA_DIR/backups"

DATA_DIR="$DATA_DIR" STAMP="$STAMP" "$PYTHON" - <<'PY'
import os
import sqlite3

data = os.environ["DATA_DIR"]
stamp = os.environ["STAMP"]
name = f"backups/pipeline-{stamp}.db"

source = sqlite3.connect(f"{data}/pipeline.db")
target = sqlite3.connect(f"{data}/{name}")
with target:
    source.backup(target)
target.close()
source.close()
print(f"backed up to {name}")
PY

# Keep the newest few. Enough to survive a bad week; not enough to fill a disk.
ls -1t "$DATA_DIR"/backups/pipeline-*.db 2>/dev/null \
    | tail -n "+$((KEEP + 1))" \
    | while IFS= read -r old; do rm -f -- "$old"; done

echo "$(ls -1 "$DATA_DIR"/backups/pipeline-*.db 2>/dev/null | wc -l) backup(s) kept."
