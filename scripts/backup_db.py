"""Nightly online backup of opsbot.db; run from /etc/cron.d/offsight-bot-backup as offsightbot.

SQLite's backup API copies a consistent snapshot while the bot keeps running. Only this script's
dated files (opsbot-YYYY-MM-DD.db) are pruned, so hand-made backups in the folder are kept.
"""
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SOURCE = Path("/opt/offsight-bot/data/opsbot.db")
BACKUPS = Path("/opt/offsight-bot/backups")
KEEP_DAYS = 14
NIGHTLY = re.compile(r"opsbot-(\d{4}-\d{2}-\d{2})\.db")


def main() -> int:
    today = date.today()
    target = BACKUPS / f"opsbot-{today.isoformat()}.db"
    partial = target.with_suffix(".db.partial")
    source = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True)
    copy = sqlite3.connect(partial)
    try:
        source.backup(copy)
        if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup failed its integrity check")
    finally:
        copy.close()
        source.close()
    partial.replace(target)
    for old in BACKUPS.iterdir():
        match = NIGHTLY.fullmatch(old.name)
        if match and date.fromisoformat(match[1]) < today - timedelta(days=KEEP_DAYS):
            old.unlink()
    print(f"Backed up {SOURCE} to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
