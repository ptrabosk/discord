#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/offsight-bot/app"
DATA_DIR="/opt/offsight-bot/data"
BACKUP_DIR="/opt/offsight-bot/backups"
SERVICE_FILE="/etc/systemd/system/offsight-bot.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

if [[ ! -f "${APP_DIR}/requirements.txt" || ! -f "${APP_DIR}/deploy/offsight-bot.service" ]]; then
  echo "Clone the repository into ${APP_DIR} before running this installer." >&2
  exit 1
fi

if [[ -e "${SERVICE_FILE}" ]]; then
  echo "Refusing to overwrite existing service file: ${SERVICE_FILE}" >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv git
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else "Python 3.9 or newer is required")'

if ! getent group offsightbot >/dev/null 2>&1; then
  groupadd --system offsightbot
fi

if ! id -u offsightbot >/dev/null 2>&1; then
  useradd --system --gid offsightbot --home-dir /opt/offsight-bot --shell /usr/sbin/nologin offsightbot
fi

install -d -o offsightbot -g offsightbot -m 0750 "${DATA_DIR}" "${BACKUP_DIR}"

if [[ -e "${APP_DIR}/.venv" ]]; then
  echo "Refusing to overwrite existing virtual environment: ${APP_DIR}/.venv" >&2
  exit 1
fi

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

chown -R root:offsightbot "${APP_DIR}"
chmod 0750 "${APP_DIR}"

for secret_file in "${APP_DIR}/.env" "${APP_DIR}/credentials.json"; do
  if [[ -e "${secret_file}" ]]; then
    chown offsightbot:offsightbot "${secret_file}"
    chmod 0600 "${secret_file}"
  fi
done

install -o root -g root -m 0644 "${APP_DIR}/deploy/offsight-bot.service" "${SERVICE_FILE}"
systemctl daemon-reload

if [[ -f "${APP_DIR}/.env" ]] && (
  cd "${APP_DIR}"
  runuser -u offsightbot -- "${APP_DIR}/.venv/bin/python" -c "import main"
); then
  systemctl enable offsight-bot
  echo "Configuration validation passed and offsight-bot was enabled at boot."
else
  echo "Configuration is incomplete or invalid; offsight-bot was not enabled or started." >&2
  echo "Finish ${APP_DIR}/.env, credentials, and database setup, then validate and enable it manually." >&2
fi

echo "The bot was not started. Start it only after the Windows/local bot is stopped."
