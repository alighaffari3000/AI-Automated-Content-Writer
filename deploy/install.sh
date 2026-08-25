#!/bin/bash
# Put the pipeline on a server, or update it after a `git pull`.
#
# Safe to run again: it fetches the image, installs the units and leaves the
# database and the environment file exactly where they were. Nothing here
# deletes anything.
#
#   sudo ./deploy/install.sh
#
# The image is built by GitHub Actions and pulled from there, because
# `docker build` is the heaviest thing that would ever happen on a machine
# whose actual job is a two-minute run once a day. To build it here anyway —
# no network to the registry, or a change not pushed yet:
#
#   sudo BUILD_LOCALLY=1 ./deploy/install.sh
#
# To pin a particular build instead of following latest:
#
#   sudo IMAGE=ghcr.io/alighaffari3000/ai-content-writer:sha-1a2b3c4 ./deploy/install.sh
#
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/alighaffari3000/ai-content-writer:latest}"
BUILD_LOCALLY="${BUILD_LOCALLY:-0}"
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

if [ "$BUILD_LOCALLY" = "1" ]; then
    echo "==> Building $IMAGE here"
    docker build -t "$IMAGE" "$root"
else
    echo "==> Fetching $IMAGE"
    if ! docker pull "$IMAGE"; then
        cat >&2 <<EOF

Could not pull $IMAGE.

Two things it usually is:
  * The package is still private. Make it public once, at
    https://github.com/users/alighaffari3000/packages/container/ai-content-writer/settings
    (Danger Zone -> Change visibility -> Public). The repository being public
    does not make its images public.
  * No build has finished yet. Check the Actions tab; the workflow publishes
    :latest from main, and from a branch when you dispatch it with
    "tag_as_latest".

Or build it on this machine instead: sudo BUILD_LOCALLY=1 $0
EOF
        exit 1
    fi
fi

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

# The unit and the backup script both name the image; both get it from here,
# so the image that runs and the image that was pulled cannot drift apart.
fill_in() {
    sed "s|__IMAGE__|$IMAGE|g" "$1"
}

echo "==> Installing the backup script"
mkdir -p "$LIB_DIR"
fill_in "$here/backup.sh" >"$LIB_DIR/backup.sh"
chmod 755 "$LIB_DIR/backup.sh"

echo "==> Installing the units"
for unit in content-writer.service content-writer.timer \
            content-writer-backup.service content-writer-backup.timer; do
    fill_in "$here/$unit" >"$UNIT_DIR/$unit"
    chmod 644 "$UNIT_DIR/$unit"
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
