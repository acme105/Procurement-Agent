#!/usr/bin/env bash
# Install or update the app on an Ubuntu/Debian server. Run as root (or with sudo):  bash deploy/setup.sh
# Installs Python and Chrome (for PDFs), creates a locked-down service user and a systemd service. It does NOT touch
# any web server: put your own reverse proxy (nginx, Caddy) in front, see deploy/nginx.example.conf.
#   APP_DIR (default /opt/aerchain)  HOST (default 127.0.0.1)  PORT (default 8085)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/aerchain}"
REPO="${REPO:-https://github.com/acme105/Procurement-Agent.git}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8085}"

apt-get install -y python3-venv git curl ca-certificates fonts-liberation >/dev/null
CHROME_BIN="$(command -v google-chrome || true)"
if [ -z "$CHROME_BIN" ]; then
  if [ "$(dpkg --print-architecture)" = "amd64" ]; then
    curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt-get install -y /tmp/chrome.deb >/dev/null && rm -f /tmp/chrome.deb
    CHROME_BIN="$(command -v google-chrome)"
  else
    apt-get install -y chromium >/dev/null || apt-get install -y chromium-browser >/dev/null
    CHROME_BIN="$(command -v chromium || command -v chromium-browser)"
  fi
fi

id aerchain >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin aerchain
mkdir -p "$APP_DIR" /etc/aerchain
chown aerchain:aerchain "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then sudo -u aerchain git -C "$APP_DIR" pull --ff-only; else sudo -u aerchain git clone "$REPO" "$APP_DIR"; fi
sudo -u aerchain python3 -m venv "$APP_DIR/.venv"
grep -v '^pytest' "$APP_DIR/requirements.txt" > /tmp/aerchain-req.txt
sudo -u aerchain "$APP_DIR/.venv/bin/pip" install -q --no-cache-dir -r /tmp/aerchain-req.txt && rm -f /tmp/aerchain-req.txt

# secrets live only in this root-only file, never in the repo (see .env.example for the names)
[ -f /etc/aerchain/aerchain.env ] || install -m 600 -o root -g root /dev/null /etc/aerchain/aerchain.env
grep -q '^AERCHAIN_CHROME=' /etc/aerchain/aerchain.env || printf 'AERCHAIN_CHROME=%s\nAERCHAIN_CHROME_NO_SANDBOX=1\nAERCHAIN_STATE=%s/state\n' "$CHROME_BIN" "$APP_DIR" >> /etc/aerchain/aerchain.env

sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__HOST__#$HOST#g" -e "s#__PORT__#$PORT#g" "$APP_DIR/deploy/aerchain.service" > /etc/systemd/system/aerchain.service
systemctl daemon-reload
systemctl enable aerchain >/dev/null
systemctl restart aerchain
echo "Installed. Check: curl -s http://$HOST:$PORT/api/health   (open; details are at /api/health/details after signing in)"
