from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from forms.domain import VALID_STATUSES, submission_prefix

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "opsbot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS form_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT UNIQUE,
    interaction_id TEXT NOT NULL UNIQUE,
    form_type TEXT NOT NULL,
    form_subtype TEXT,
    discord_user_id INTEGER NOT NULL,
    discord_username TEXT NOT NULL,
    discord_display_name TEXT NOT NULL,
    employee_email TEXT NOT NULL,
    employee_name TEXT NOT NULL,
    submitted_at_utc TEXT NOT NULL,
    submitted_at_et TEXT NOT NULL,
    category TEXT,
    adjustment_type TEXT,
    start_time_et TEXT,
    start_time_utc TEXT,
    end_time_et TEXT,
    end_time_utc TEXT,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending',
    reviewed_by_discord_id INTEGER,
    reviewed_by_email TEXT,
    reviewed_at_utc TEXT,
    reviewed_at_et TEXT,
    sheets_sync_status TEXT NOT NULL DEFAULT 'PENDING',
    sheets_synced_at TEXT,
    sheets_last_error TEXT,
    review_channel_id INTEGER,
    review_message_id INTEGER UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_form_submissions_sheets_sync
ON form_submissions(sheets_sync_status, id);
"""


class FormsRepository:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)

    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def init(self):
        with self.connect() as con:
            con.executescript(SCHEMA)

    def create(self, values: dict) -> tuple[dict, bool]:
        submitted_et = datetime.fromisoformat(values["submitted_at_et"])
        now = datetime.now(timezone.utc).isoformat()
        columns = (
            "interaction_id", "form_type", "form_subtype", "discord_user_id",
            "discord_username", "discord_display_name", "employee_email", "employee_name",
            "submitted_at_utc", "submitted_at_et", "category", "adjustment_type",
            "start_time_et", "start_time_utc", "end_time_et", "end_time_utc", "message",
        )
        try:
            with self.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                cursor = con.execute(
                    f"INSERT INTO form_submissions({','.join(columns)},created_at,updated_at) "
                    f"VALUES({','.join('?' for _ in columns)},?,?)",
                    tuple(values.get(column) for column in columns) + (now, now),
                )
                submission_id = (
                    f"{submission_prefix(values['form_type'], values.get('form_subtype'))}-"
                    f"{submitted_et:%Y%m%d}-{cursor.lastrowid:04d}"
                )
                con.execute(
                    "UPDATE form_submissions SET submission_id=? WHERE id=?",
                    (submission_id, cursor.lastrowid),
                )
                row = con.execute(
                    "SELECT * FROM form_submissions WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                return dict(row), True
        except sqlite3.IntegrityError:
            existing = self.get_by_interaction(values["interaction_id"])
            if existing:
                return existing, False
            raise

    def get(self, submission_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM form_submissions WHERE submission_id=?", (submission_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_by_interaction(self, interaction_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM form_submissions WHERE interaction_id=?", (interaction_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_by_review_message(self, message_id: int) -> dict | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM form_submissions WHERE review_message_id=?", (message_id,)
            ).fetchone()
        return dict(row) if row else None

    def pending_sync(self, limit: int = 25) -> list[dict]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM form_submissions WHERE sheets_sync_status != 'SYNCED' "
                "ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_synced(self, submission_id: str):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as con:
            con.execute(
                "UPDATE form_submissions SET sheets_sync_status='SYNCED', sheets_synced_at=?, "
                "sheets_last_error=NULL, updated_at=? WHERE submission_id=?",
                (now, now, submission_id),
            )

    def mark_sync_failed(self, submission_id: str, error: str):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as con:
            con.execute(
                "UPDATE form_submissions SET sheets_sync_status='PENDING', sheets_last_error=?, "
                "updated_at=? WHERE submission_id=?",
                (error[:1000], now, submission_id),
            )

    def set_review_message(self, submission_id: str, channel_id: int, message_id: int):
        with self.connect() as con:
            con.execute(
                "UPDATE form_submissions SET review_channel_id=?, review_message_id=?, updated_at=? "
                "WHERE submission_id=?",
                (channel_id, message_id, datetime.now(timezone.utc).isoformat(), submission_id),
            )

    def review(
        self,
        submission_id: str,
        status: str,
        reviewer_id: int,
        reviewer_email: str | None,
        reviewed_at_utc: str,
        reviewed_at_et: str,
    ) -> dict:
        if status not in VALID_STATUSES - {"Pending"}:
            raise ValueError("Invalid review status.")
        with self.connect() as con:
            con.execute(
                "UPDATE form_submissions SET status=?, reviewed_by_discord_id=?, "
                "reviewed_by_email=?, reviewed_at_utc=?, reviewed_at_et=?, "
                "sheets_sync_status='PENDING', sheets_last_error=NULL, updated_at=? "
                "WHERE submission_id=?",
                (
                    status, reviewer_id, reviewer_email, reviewed_at_utc, reviewed_at_et,
                    datetime.now(timezone.utc).isoformat(), submission_id,
                ),
            )
        return self.get(submission_id)
