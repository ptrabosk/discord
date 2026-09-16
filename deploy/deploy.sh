#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/offsight-bot/app"
SERVICE_NAME="offsight-bot"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this deployment as root: sudo bash ${APP_DIR}/deploy/deploy.sh" >&2
  exit 1
fi

cd "${APP_DIR}"

if [[ ! -f ".env" ]]; then
  echo "Refusing to deploy: ${APP_DIR}/.env is missing." >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Refusing to deploy: ${APP_DIR}/.venv is missing or incomplete." >&2
  exit 1
fi

git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m compileall -q main.py app_paths.py bot database integrations
.venv/bin/python -c "import app_paths, bot.commands, bot.guild_authorization, bot.health, bot.instance_lock, bot.onboarding, bot.permissions, bot.setup_server, bot.sync, database.database, integrations.email_verification, integrations.google_sheets"
runuser -u offsightbot -- .venv/bin/python -c "import main"

systemctl restart "${SERVICE_NAME}"
sleep 2

if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
  systemctl status "${SERVICE_NAME}" --no-pager --lines=30 || true
  journalctl -u "${SERVICE_NAME}" -n 100 --no-pager || true
  exit 1
fi

systemctl status "${SERVICE_NAME}" --no-pager --lines=20
journalctl -u "${SERVICE_NAME}" -n 50 --no-pager
