#!/bin/bash
# Put the pipeline on a server, or update it after a `git pull`.
#
#   sudo ./deploy/install.sh
#
# Safe to run again: it replaces the code and the environment it runs in, and
# leaves the database and the configuration exactly where they were. Nothing
# here deletes anything you wrote.
#
# No container. This is a two-minute job once a day, and a container runtime
# would be a daemon running all day to host it. The dependencies are prebuilt
# wheels — about 280 MB, downloaded once by uv, no compiler involved.
#
# It ends up as three directories, each with one job:
#
#   /opt/content-writer      the code and its virtualenv, replaced on update
#   /var/lib/content-writer  the database and its backups, never touched
#   /etc/content-writer      the keys and the settings, never touched
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/content-writer}"
DATA_DIR="${DATA_DIR:-/var/lib/content-writer}"
ENV_DIR="${ENV_DIR:-/etc/content-writer}"
LIB_DIR="${LIB_DIR:-/usr/local/lib/content-writer}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
UV="${UV:-/usr/local/bin/uv}"
SERVICE_USER="${SERVICE_USER:-contentwriter}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"

if [ "$(id -u)" -ne 0 ]; then
    echo "This installs into /opt, /etc and /var/lib — run it with sudo." >&2
    exit 1
fi

echo "==> Service user: $SERVICE_USER"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    # A system account with a home under /opt, because uv keeps its own files
    # in HOME and the service must own everything it reads.
    useradd --system --create-home --home-dir "$APP_DIR" \
            --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "    Created."
else
    echo "    Already exists."
fi

echo "==> uv"
if [ ! -x "$UV" ]; then
    if command -v uv >/dev/null 2>&1; then
        UV="$(command -v uv)"
        echo "    Using $UV"
    else
        echo "    Installing to /usr/local/bin"
        curl -LsSf https://astral.sh/uv/install.sh \
            | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
    fi
else
    echo "    Already installed: $("$UV" --version)"
fi

echo "==> Copying the code to $APP_DIR"
mkdir -p "$APP_DIR"
# The checkout, minus everything that belongs to the machine rather than to the
# code. --delete so a file removed upstream stops existing here too, and the
# virtualenv is protected from it because uv rebuilds it in the next step.
tar -C "$root" \
    --exclude=.git --exclude=.venv --exclude=data --exclude=logs \
    --exclude=__pycache__ --exclude='*.pyc' \
    -cf - . | tar -C "$APP_DIR" -xf -
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

echo "==> Building the environment (this is the slow step, once)"
sudo -u "$SERVICE_USER" env \
    HOME="$APP_DIR" UV_CACHE_DIR=/tmp/uv-cache \
    "$UV" sync --frozen --project "$APP_DIR"

echo "==> Preparing $DATA_DIR"
mkdir -p "$DATA_DIR/backups"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
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
# It holds the API keys and the site's token: readable by the service, by
# nobody else.
chown "root:$SERVICE_USER" "$ENV_DIR/env"
chmod 640 "$ENV_DIR/env"
# The application reads .env from its working directory, exactly as it does on
# a laptop. One file, one format, no second way of configuring things.
ln -sfn "$ENV_DIR/env" "$APP_DIR/.env"
chown -h "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"

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
       sudo -u $SERVICE_USER env HOME=$APP_DIR DB_PATH=$DATA_DIR/pipeline.db \\
           $UV run --no-sync --project $APP_DIR python -m app.cli check
  3. Give it something to write about, with the same prefix:
       ... python -m app.cli categories seed
  4. Try one run — DRY_RUN is on, so it publishes nothing:
       sudo systemctl start content-writer.service
       journalctl -u content-writer.service -n 200 --no-pager
EOF
else
    echo "Updated. The next run happens on schedule; to test now:"
    echo "  sudo systemctl start content-writer.service"
fi
