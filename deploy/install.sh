#!/usr/bin/env bash
# Run as the user that will own finmon (on a server or your own computer). Works from any location.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env — fill it in."; }
chmod 600 .env

mkdir -p ~/.config/systemd/user
for unit in finmon-run.service finmon-run.timer finmon-bot.service; do
  sed "s#@PROJECT_DIR@#$PROJECT_DIR#g" "deploy/$unit" > ~/.config/systemd/user/$unit
done
systemctl --user daemon-reload
# On a server, keep user services running without a login session (not needed on a desktop):
[ "${FINMON_SERVER:-0}" = 1 ] && sudo loginctl enable-linger "$USER"
echo "Installed. When .env is complete and the bank is linked, enable with:"
echo "  systemctl --user enable --now finmon-run.timer finmon-bot.service"
