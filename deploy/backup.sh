#!/usr/bin/env bash
set -euo pipefail
umask 077

APP_DIR="/opt/offsight-bot/app"
DATABASE_PATH="${1:-/opt/offsight-bot/data/opsbot.db}"
BACKUP_DIR="${2:-/opt/offsight-bot/backups}"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"
TIMESTAMP="$(date -u +%Y-%m-%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/opsbot-${TIMESTAMP}.db"
TEMP_FILE=""

cleanup() {
  if [[ -n "${TEMP_FILE}" && -e "${TEMP_FILE}" ]]; then
    rm -f -- "${TEMP_FILE}"
  fi
}
trap cleanup EXIT

if [[ ! -f "${DATABASE_PATH}" ]]; then
  echo "Database not found: ${DATABASE_PATH}" >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Python virtual environment not found: ${PYTHON_BIN}" >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"
if [[ -e "${BACKUP_FILE}" ]]; then
  echo "Backup already exists: ${BACKUP_FILE}" >&2
  exit 1
fi
TEMP_FILE="$(mktemp "${BACKUP_DIR}/.opsbot-${TIMESTAMP}.XXXXXX.tmp")"

"${PYTHON_BIN}" -c '
import sqlite3
import sys
from contextlib import closing

source_path, backup_path = sys.argv[1:]
with closing(sqlite3.connect(source_path, timeout=30)) as source:
    with closing(sqlite3.connect(backup_path)) as destination:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError("SQLite integrity check failed")
' "${DATABASE_PATH}" "${TEMP_FILE}"

mv -- "${TEMP_FILE}" "${BACKUP_FILE}"
TEMP_FILE=""

echo "SQLite backup created: ${BACKUP_FILE}"
