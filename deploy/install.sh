#!/bin/bash
# Put the pipeline on a server, or update it after a `git pull`.
#
# Safe to run again: it builds the image, installs the units and leaves the
# database and the environment file exactly where they were. Nothing here
# deletes anything.
#
#   sudo ./deploy/install.sh
#
set -euo pipefail

IMAGE="${IMAGE:-ai-content-writer:latest}"
DATA_DIR="${DATA_DIR:-/var/lib/content-writer}"
ENV_DIR="${ENV_DIR:-/etc/content-writer}"
LIB_DIR="${LIB_DIR:-/usr/local/lib/content-writer}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
# Matches the user inside the image, so the container can write the database
# on a volume the host also understands.
CONTAINER_UID="${CONTAINER_UID:-10001}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"

if [ "$(id -u)" -ne 0 ]; then
    echo "This installs into /etc and /var/lib — run it with sudo." >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed. Install it first: https://docs.docker.com/engine/install/" >&2
    exit 1
fi

echo "==> Building $IMAGE"
docker build -t "$IMAGE" "$root"

echo "==> Preparing $DATA_DIR"
mkdir -p "$DATA_DIR/backups"
chown -R "$CONTAINER_UID:$CONTAINER_UID" "$DATA_DIR"
chmod 750 "$DATA_DIR"

echo "==> Preparing $ENV_DIR/env"
mkdir -p "$ENV_DIR"
if [ ! -f "$ENV_DIR/env" ]; then
    cp "$root/.env.example" "$ENV_DIR/env"
    # A fresh install must not be able to publish to a live site by accident,
    # whatever the example file happened to say. Turning this off is a decision
    # someone makes after reading an article the pipeline wrote.
    if grep -q '^DRY_RUN=' "$ENV_DIR/env"; then
        sed -i 's/^DRY_RUN=.*/DRY_RUN=true/' "$ENV_DIR/env"
    else
        printf '\nDRY_RUN=true\n' >>"$ENV_DIR/env"
    fi
    echo "    Created from .env.example, with DRY_RUN=true so nothing can be"
    echo "    published until you say so. It still has placeholder values in it."
    NEEDS_EDITING=1
else
    echo "    Already there; left untouched."
    NEEDS_EDITING=0
fi
# It holds the API keys and the site's token, so it is not world-readable — but
# the container runs as an unprivileged user and has to read it, so the group
# is the container's own uid rather than root's.
chown "root:$CONTAINER_UID" "$ENV_DIR/env"
chmod 640 "$ENV_DIR/env"

echo "==> Installing the backup script"
install -D -m 755 "$here/backup.sh" "$LIB_DIR/backup.sh"

echo "==> Installing the units"
for unit in content-writer.service content-writer.timer \
            content-writer-backup.service content-writer-backup.timer; do
    install -m 644 "$here/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload

echo "==> Enabling the timers"
systemctl enable --now content-writer.timer content-writer-backup.timer

echo
systemctl list-timers 'content-writer*' --no-pager || true
echo

if [ "$NEEDS_EDITING" -eq 1 ]; then
    cat <<EOF
Next, and nothing will work until this is done:

  1. Fill in $ENV_DIR/env — at minimum GEMINI_API_KEY, SITE_API_URL and
     SITE_API_TOKEN.
  2. Check what the pipeline sees:
       sudo docker run --rm -v $DATA_DIR:/code/data -v $ENV_DIR/env:/code/.env:ro \\
           $IMAGE uv run --no-sync python -m app.cli check
  3. Give it something to write about:
       ... app.cli categories seed
  4. Try one run without publishing anything: set DRY_RUN=true, then
       sudo systemctl start content-writer.service
       journalctl -u content-writer.service -n 200 --no-pager
EOF
else
    echo "Updated. The next run happens on schedule; to test now:"
    echo "  sudo systemctl start content-writer.service"
fi
