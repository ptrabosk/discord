"""Durable reachout workflow state in the bot's SQLite database.

Every state change that must not be lost or repeated happens inside one BEGIN IMMEDIATE
transaction together with the jobs (Discord posts, DMs) it implies, so a crash leaves either
both or neither.
"""
from __future__ import annotations

import random
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from database.forms_repository import DEFAULT_DB_PATH
from reachouts.domain import (
    ACKNOWLEDGMENT,
    FOLLOW_UP,
    OPEN_OBLIGATION_STATES,
    ReachoutConfig,
    iso,
    new_id,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reachout_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reachout_agents (
    agent_id TEXT PRIMARY KEY,
    agent_email TEXT,
    agent_name TEXT,
    agent_discord_user_id INTEGER,
    lead_discord_user_id INTEGER,
    active INTEGER NOT NULL DEFAULT 0,
    team TEXT,
    timezone TEXT,
    fallback_channel_id INTEGER,
    existing_channel_id INTEGER,
    private_channel_id INTEGER,
    channel_lead_discord_user_id INTEGER,
    directory_status TEXT NOT NULL,
    directory_error TEXT,
    in_sheet INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reachouts (
    reachout_id TEXT PRIMARY KEY,
    dispatch_state TEXT NOT NULL,
    workflow_status TEXT NOT NULL DEFAULT 'NOT_SENT',
    status_detail TEXT,
    assigned_hash TEXT,
    input_hash TEXT,
    snapshot_json TEXT,
    snapshot_hash TEXT,
    external_source_id TEXT UNIQUE,
    agent_id TEXT,
    reachout_type TEXT,
    ack_required INTEGER,
    text_required INTEGER,
    agent_discord_user_id INTEGER,
    channel_id INTEGER,
    message_id INTEGER,
    thread_id INTEGER,
    prepared_at TEXT,
    created_at TEXT NOT NULL,
    queued_at TEXT,
    delivered_at TEXT,
    due_at TEXT,
    ack_status TEXT,
    ack_at TEXT,
    written_status TEXT,
    latest_agent_response_at TEXT,
    resolved_at TEXT,
    cancelled_at TEXT,
    last_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    row_missing INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reachout_obligations (
    obligation_id TEXT PRIMARY KEY,
    reachout_id TEXT NOT NULL REFERENCES reachouts(reachout_id),
    obligation_type TEXT NOT NULL,
    text_required INTEGER NOT NULL,
    requested_by_discord_user_id INTEGER,
    requesting_response_id TEXT,
    required_agent_discord_user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    due_at TEXT,
    satisfied_by_response_id TEXT,
    satisfied_at TEXT,
    superseded_by TEXT,
    closed_at TEXT,
    next_nudge_at TEXT,
    nudge_count INTEGER NOT NULL DEFAULT 0,
    reminder_count INTEGER NOT NULL DEFAULT 0,
    last_reminder_at TEXT,
    last_reminder_by INTEGER,
    sheet_dirty INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reachout_one_open_obligation
ON reachout_obligations(reachout_id) WHERE status IN ('AWAITING_DELIVERY', 'PENDING');
CREATE TABLE IF NOT EXISTS reachout_responses (
    response_id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL UNIQUE,
    reachout_id TEXT NOT NULL REFERENCES reachouts(reachout_id),
    obligation_id TEXT,
    actor_discord_user_id INTEGER NOT NULL,
    actor_name TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    response_type TEXT NOT NULL,
    acknowledgment INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',
    submitted_at TEXT NOT NULL,
    in_reply_to_response_id TEXT,
    created_obligation_id TEXT,
    review_status TEXT,
    reviewed_by_discord_user_id INTEGER,
    reviewed_at TEXT,
    review_message_id INTEGER,
    prepared_at TEXT,
    thread_message_id INTEGER,
    thread_post_status TEXT NOT NULL DEFAULT 'QUEUED',
    lead_notify_status TEXT,
    lead_notified_discord_user_id INTEGER,
    lead_dm_channel_id INTEGER,
    lead_dm_message_id INTEGER,
    last_error TEXT,
    sheet_dirty INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reachout_nudges (
    nudge_id TEXT PRIMARY KEY,
    obligation_id TEXT NOT NULL,
    reachout_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    lead_discord_user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    handled_action TEXT,
    handled_by_discord_user_id INTEGER,
    handled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(obligation_id, seq)
);
CREATE TABLE IF NOT EXISTS reachout_events (
    event_id TEXT PRIMARY KEY,
    dedupe_key TEXT UNIQUE,
    reachout_id TEXT,
    response_id TEXT,
    obligation_id TEXT,
    event_type TEXT NOT NULL,
    actor_discord_user_id INTEGER,
    actor_role TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    details TEXT,
    sheet_dirty INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reachout_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    target TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    in_flight INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    lease_until TEXT,
    last_error TEXT,
    alerted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reachout_jobs_due ON reachout_jobs(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_reachout_obligations_nudge ON reachout_obligations(status, next_nudge_at);
"""

# Dispatch states (the row has not reached Discord yet unless DELIVERED).
NOT_DELIVERED_STATES = ("ID_ASSIGNED", "INVALID", "BLOCKED", "QUEUED", "WITHDRAWN", "HELD_CHANGED")
SHEET_TABLES = {
    "responses": ("reachout_responses", "response_id"),
    "obligations": ("reachout_obligations", "obligation_id"),
    "events": ("reachout_events", "event_id"),
}


class WorkflowError(Exception):
    """An action that is not allowed in the reachout's current state; the message is user-facing."""


class StaleForm(WorkflowError):
    """The form was opened for an obligation that is no longer the open one."""


def _dict(row):
    return dict(row) if row else None


class ReachoutRepository:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)

    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=15000")
        return con

    def init(self):
        con = self.connect()
        try:
            con.executescript(SCHEMA)
            columns = {row["name"] for row in con.execute("PRAGMA table_info(reachout_agents)")}
            for column in ("lead_email", "agent_first_name"):
                if column not in columns:
                    con.execute(f"ALTER TABLE reachout_agents ADD COLUMN {column} TEXT")
            columns = {row["name"] for row in con.execute("PRAGMA table_info(reachout_responses)")}
            if "attachments" not in columns:
                con.execute("ALTER TABLE reachout_responses ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
        finally:
            con.close()

    @contextmanager
    def tx(self):
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def all(self, sql: str, params=()) -> list[dict]:
        con = self.connect()
        try:
            return [dict(row) for row in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    # --- helpers used inside transactions ----------------------------------------------

    @staticmethod
    def _update(con, table: str, key: str, key_value, now: datetime, **fields):
        fields["updated_at"] = iso(now)
        if table in {t for t, _ in SHEET_TABLES.values()}:
            fields["sheet_dirty"] = 1
        assignments = ",".join(f"{name}=?" for name in fields)
        con.execute(f"UPDATE {table} SET {assignments} WHERE {key}=?", (*fields.values(), key_value))

    @staticmethod
    def _event(con, now: datetime, event_type: str, *, actor_role="BOT", actor=None, reachout_id=None,
               response_id=None, obligation_id=None, outcome="OK", details="", dedupe_key=None,
               channel_id=None, message_id=None) -> str | None:
        event_id = new_id("EVT", now)
        cursor = con.execute(
            "INSERT OR IGNORE INTO reachout_events(event_id,dedupe_key,reachout_id,response_id,obligation_id,"
            "event_type,actor_discord_user_id,actor_role,occurred_at,outcome,channel_id,message_id,details,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, dedupe_key, reachout_id, response_id, obligation_id, event_type, actor, actor_role,
             iso(now), outcome, channel_id, message_id, (details or "")[:2000], iso(now)),
        )
        return event_id if cursor.rowcount else None

    @staticmethod
    def _job(con, now: datetime, kind: str, ref_id: str, *, dedupe_key: str | None = None,
             target=None, rearm=False):
        """Queue one side effect. rearm re-runs an idempotent job (a message edit) that already ran."""
        dedupe_key = dedupe_key or f"{kind}:{ref_id}"
        con.execute(
            "INSERT INTO reachout_jobs(dedupe_key,kind,ref_id,target,next_attempt_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(dedupe_key) DO "
            + ("UPDATE SET status='PENDING', attempts=0, next_attempt_at=excluded.next_attempt_at, "
               "updated_at=excluded.updated_at WHERE status!='PENDING'" if rearm else "NOTHING"),
            (dedupe_key, kind, ref_id, None if target is None else str(target), iso(now), iso(now), iso(now)),
        )

    def _stale_nudges(self, con, obligation_id: str, now: datetime):
        for nudge in con.execute(
            "SELECT nudge_id,status FROM reachout_nudges WHERE obligation_id=? AND status IN ('QUEUED','SENT')",
            (obligation_id,),
        ).fetchall():
            self._update(con, "reachout_nudges", "nudge_id", nudge["nudge_id"], now, status="STALE")
            if nudge["status"] == "SENT":
                self._job(con, now, "refresh_nudge_dm", nudge["nudge_id"], rearm=True)

    # --- key/value ---------------------------------------------------------------------

    def kv_get(self, key: str) -> str | None:
        row = self.one("SELECT value FROM reachout_kv WHERE key=?", (key,))
        return row["value"] if row else None

    def kv_set(self, key: str, value: str):
        with self.tx() as con:
            con.execute(
                "INSERT INTO reachout_kv(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, iso(utcnow())),
            )

    def load_config(self) -> ReachoutConfig:
        raw = self.kv_get("config")
        try:
            return ReachoutConfig.from_json(raw) if raw else ReachoutConfig()
        except (ValueError, TypeError, KeyError):
            return ReachoutConfig()

    def save_config(self, config: ReachoutConfig):
        self.kv_set("config", config.to_json())

    def add_event(self, event_type: str, **kwargs) -> str | None:
        now = kwargs.pop("now", None) or utcnow()
        with self.tx() as con:
            return self._event(con, now, event_type, **kwargs)

    # --- agents ------------------------------------------------------------------------

    def get_agent(self, agent_id: str) -> dict | None:
        return self.one("SELECT * FROM reachout_agents WHERE agent_id=?", (agent_id,))

    def find_agent(self, key: str) -> dict | None:
        """By agent_id, or by personal email (case-insensitive)."""
        key = key.strip()
        return self.get_agent(key) or self.one(
            "SELECT * FROM reachout_agents WHERE lower(agent_email)=lower(?) AND in_sheet=1", (key,))

    def agents(self) -> list[dict]:
        return self.all("SELECT * FROM reachout_agents")

    def upsert_agent(self, values: dict, now: datetime) -> dict | None:
        """Store a directory row and return the previous record (None if new)."""
        columns = (
            "agent_email", "agent_name", "agent_first_name", "agent_discord_user_id", "lead_email", "lead_discord_user_id", "active", "team",
            "timezone", "fallback_channel_id", "existing_channel_id", "directory_status", "directory_error",
        )
        with self.tx() as con:
            previous = _dict(con.execute("SELECT * FROM reachout_agents WHERE agent_id=?", (values["agent_id"],)).fetchone())
            if previous is None:
                con.execute(
                    f"INSERT INTO reachout_agents(agent_id,{','.join(columns)},in_sheet,updated_at) "
                    f"VALUES(?,{','.join('?' for _ in columns)},1,?)",
                    (values["agent_id"], *(values.get(c) for c in columns), iso(now)),
                )
            else:
                kept = dict(values)
                # A row that fails validation must not erase who the last valid lead was.
                if kept.get("lead_discord_user_id") is None:
                    kept["lead_discord_user_id"] = previous["lead_discord_user_id"]
                if kept.get("agent_discord_user_id") is None:
                    kept["agent_discord_user_id"] = previous["agent_discord_user_id"]
                changed = {c: kept.get(c) for c in columns if kept.get(c) != previous[c]}
                if changed or not previous["in_sheet"]:
                    self._update(con, "reachout_agents", "agent_id", values["agent_id"], now, in_sheet=1, **changed)
            return previous

    def mark_agents_missing(self, present_ids: set[str], now: datetime) -> list[str]:
        missing = []
        with self.tx() as con:
            for row in con.execute("SELECT agent_id FROM reachout_agents WHERE in_sheet=1").fetchall():
                if row["agent_id"] not in present_ids:
                    missing.append(row["agent_id"])
                    self._update(con, "reachout_agents", "agent_id", row["agent_id"], now, in_sheet=0, active=0,
                                 directory_status="MISSING", directory_error="Row removed from Reachout Agents.")
        return missing

    def set_agent_channel(self, agent_id: str, channel_id: int, lead_id: int | None, now: datetime):
        with self.tx() as con:
            self._update(con, "reachout_agents", "agent_id", agent_id, now,
                         private_channel_id=channel_id, channel_lead_discord_user_id=lead_id)

    def reassign_lead(self, agent_id: str, old_lead: int, new_lead: int, now: datetime):
        """Route unreviewed responses and open overdue nudges to the new lead."""
        with self.tx() as con:
            self._event(con, now, "LEAD_REASSIGNED", actor_role="OPERATOR",
                        details=f"agent_id={agent_id}; old={old_lead}; new={new_lead}")
            if self.one_in(con, "SELECT private_channel_id FROM reachout_agents WHERE agent_id=?", (agent_id,)):
                self._job(con, now, "sync_channel", agent_id, rearm=True)
            for response in con.execute(
                "SELECT p.response_id FROM reachout_responses p JOIN reachouts r USING(reachout_id) "
                "WHERE r.agent_id=? AND p.actor_role='AGENT' AND p.review_status='UNREVIEWED' "
                "AND r.workflow_status IN ('OPEN','RESOLVED')", (agent_id,),
            ).fetchall():
                self._job(con, now, "lead_dm", response["response_id"],
                          dedupe_key=f"lead_dm:{response['response_id']}:{new_lead}", target=new_lead)
            for nudge in con.execute(
                "SELECT n.nudge_id, n.status, n.obligation_id FROM reachout_nudges n JOIN reachouts r USING(reachout_id) "
                "WHERE r.agent_id=? AND n.status IN ('QUEUED','SENT')", (agent_id,),
            ).fetchall():
                self._update(con, "reachout_nudges", "nudge_id", nudge["nudge_id"], now, status="STALE")
                if nudge["status"] == "SENT":
                    self._job(con, now, "refresh_nudge_dm", nudge["nudge_id"], rearm=True)
                self._update(con, "reachout_obligations", "obligation_id", nudge["obligation_id"], now,
                             next_nudge_at=iso(now))

    @staticmethod
    def one_in(con, sql, params=()):
        row = con.execute(sql, params).fetchone()
        return row[0] if row else None

    # --- reachouts ---------------------------------------------------------------------

    def get(self, reachout_id: str) -> dict | None:
        return self.one("SELECT * FROM reachouts WHERE reachout_id=?", (reachout_id,))

    def reachouts(self) -> list[dict]:
        return self.all("SELECT * FROM reachouts")

    def assign_id(self, input_hash: str, now: datetime) -> str:
        reachout_id = new_id("RO", now)
        with self.tx() as con:
            con.execute(
                "INSERT INTO reachouts(reachout_id,dispatch_state,status_detail,assigned_hash,input_hash,created_at,updated_at) "
                "VALUES(?,'ID_ASSIGNED','ID assigned; validating on the next poll.',?,?,?,?)",
                (reachout_id, input_hash, input_hash, iso(now), iso(now)),
            )
            self._event(con, now, "ID_ASSIGNED", reachout_id=reachout_id, actor_role="OPERATOR")
        return reachout_id

    def set_dispatch(self, reachout_id: str, state: str, detail: str, now: datetime, event: str | None = None, **fields):
        with self.tx() as con:
            current = con.execute("SELECT dispatch_state,status_detail FROM reachouts WHERE reachout_id=?",
                                  (reachout_id,)).fetchone()
            if current and (current["dispatch_state"], current["status_detail"]) == (state, detail) and not fields:
                return
            self._update(con, "reachouts", "reachout_id", reachout_id, now, dispatch_state=state,
                         status_detail=detail, **fields)
            if event:
                self._event(con, now, event, reachout_id=reachout_id, details=detail,
                            dedupe_key=f"{event}:{reachout_id}:{detail}"[:300],
                            outcome="REJECTED" if state in ("INVALID", "BLOCKED", "HELD_CHANGED") else "OK")

    def note_input(self, reachout_id: str, input_hash: str, now: datetime):
        with self.tx() as con:
            con.execute("UPDATE reachouts SET input_hash=?, row_missing=0 WHERE reachout_id=? AND "
                        "(input_hash IS NOT ? OR row_missing!=0)", (input_hash, reachout_id, input_hash))

    def queue(self, reachout_id: str, payload, snapshot_hash: str, agent: dict, now: datetime):
        """Snapshot a validated READY row and queue its delivery."""
        with self.tx() as con:
            if payload.external_source_id:
                other = con.execute(
                    "SELECT reachout_id FROM reachouts WHERE external_source_id=? AND reachout_id!=?",
                    (payload.external_source_id, reachout_id),
                ).fetchone()
                if other:
                    raise WorkflowError(f"external_source_id is already used by {other['reachout_id']}.")
            self._update(
                con, "reachouts", "reachout_id", reachout_id, now,
                dispatch_state="QUEUED", status_detail="Validated and queued for delivery.",
                snapshot_json=payload.to_json(), snapshot_hash=snapshot_hash,
                external_source_id=payload.external_source_id, agent_id=payload.agent_id,
                reachout_type=payload.reachout_type, ack_required=int(payload.ack_required),
                text_required=int(payload.response_text_required),
                agent_discord_user_id=agent["agent_discord_user_id"], queued_at=iso(now),
                last_error=None, attempts=0, next_attempt_at=None,
            )
            self._event(con, now, "QUEUED", reachout_id=reachout_id, actor_role="OPERATOR",
                        details=f"snapshot={snapshot_hash}")
            self._job(con, now, "dispatch", reachout_id, rearm=True)

    def withdraw(self, reachout_id: str, now: datetime):
        with self.tx() as con:
            self._update(con, "reachouts", "reachout_id", reachout_id, now, dispatch_state="WITHDRAWN",
                         status_detail="send is not ticked; nothing was sent.",
                         snapshot_json=None, snapshot_hash=None, external_source_id=None)
            self._event(con, now, "WITHDRAWN", reachout_id=reachout_id, actor_role="OPERATOR",
                        dedupe_key=f"WITHDRAWN:{reachout_id}:{iso(now)}")

    def cancel_before_send(self, reachout_id: str, now: datetime):
        with self.tx() as con:
            self._update(con, "reachouts", "reachout_id", reachout_id, now, dispatch_state="CANCELLED",
                         workflow_status="CANCELLED", cancelled_at=iso(now),
                         status_detail="Cancelled before delivery; nothing was sent.")
            self._event(con, now, "CANCELLED_BEFORE_SEND", reachout_id=reachout_id, actor_role="OPERATOR")

    def mark_sending(self, reachout_id: str, now: datetime) -> dict:
        """Record the send attempt (and its deadline basis) before calling Discord."""
        with self.tx() as con:
            row = _dict(con.execute("SELECT * FROM reachouts WHERE reachout_id=?", (reachout_id,)).fetchone())
            if row["dispatch_state"] == "QUEUED":
                self._update(con, "reachouts", "reachout_id", reachout_id, now, dispatch_state="SENDING",
                             prepared_at=iso(now), status_detail="Posting to Discord.")
                row = _dict(con.execute("SELECT * FROM reachouts WHERE reachout_id=?", (reachout_id,)).fetchone())
            return row

    def record_dispatch_error(self, reachout_id: str, error: str, retry_at: datetime | None, now: datetime):
        with self.tx() as con:
            self._update(con, "reachouts", "reachout_id", reachout_id, now, last_error=error[:500],
                         attempts=(self.one_in(con, "SELECT attempts FROM reachouts WHERE reachout_id=?", (reachout_id,)) or 0) + 1,
                         next_attempt_at=iso(retry_at))

    def block_dispatch(self, reachout_id: str, detail: str, now: datetime):
        """An environment problem found while sending; the next poll re-checks and re-queues."""
        with self.tx() as con:
            row = con.execute("SELECT dispatch_state FROM reachouts WHERE reachout_id=?", (reachout_id,)).fetchone()
            if row and row["dispatch_state"] in ("QUEUED", "SENDING"):
                self._update(con, "reachouts", "reachout_id", reachout_id, now, dispatch_state="BLOCKED",
                             status_detail=detail, prepared_at=None)
                self._event(con, now, "DISPATCH_BLOCKED", reachout_id=reachout_id, outcome="REJECTED",
                            details=detail, dedupe_key=f"DISPATCH_BLOCKED:{reachout_id}:{detail}"[:300])

    def mark_delivered(self, reachout_id: str, *, channel_id: int, message_id: int, delivered_at: datetime,
                       due_at: datetime | None, due_note: str, now: datetime):
        with self.tx() as con:
            row = _dict(con.execute("SELECT * FROM reachouts WHERE reachout_id=?", (reachout_id,)).fetchone())
            if row["dispatch_state"] == "DELIVERED":
                return
            cancelled = row["workflow_status"] == "CANCELLED"
            self._update(
                con, "reachouts", "reachout_id", reachout_id, now,
                dispatch_state="DELIVERED", workflow_status="CANCELLED" if cancelled else "OPEN",
                status_detail=("Delivered. " + due_note).strip(), channel_id=channel_id, message_id=message_id,
                delivered_at=iso(delivered_at), due_at=iso(due_at), last_error=None, next_attempt_at=None,
                ack_status="PENDING" if row["ack_required"] else "NOT_REQUIRED",
                written_status="PENDING" if row["text_required"] else "NOT_REQUIRED",
            )
            self._event(con, now, "DELIVERED", reachout_id=reachout_id, channel_id=channel_id,
                        message_id=message_id, details=due_note)
            if row["ack_required"]:
                obligation_id = new_id("OBL", now)
                con.execute(
                    "INSERT INTO reachout_obligations(obligation_id,reachout_id,obligation_type,text_required,"
                    "required_agent_discord_user_id,status,created_at,delivered_at,due_at,next_nudge_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (obligation_id, reachout_id, ACKNOWLEDGMENT, row["text_required"], row["agent_discord_user_id"],
                     "CANCELLED" if cancelled else "PENDING", iso(now), iso(delivered_at), iso(due_at),
                     iso(due_at), iso(now)),
                )
            self._job(con, now, "update_original", reachout_id, rearm=True)

    def set_thread(self, reachout_id: str, thread_id: int, now: datetime, recreated=False):
        with self.tx() as con:
            self._update(con, "reachouts", "reachout_id", reachout_id, now, thread_id=thread_id, last_error=None,
                         next_attempt_at=None)
            self._event(con, now, "THREAD_RECREATED" if recreated else "THREAD_CREATED",
                        reachout_id=reachout_id, channel_id=thread_id)
            self._job(con, now, "update_original", reachout_id, rearm=True)

    def mark_rows_missing(self, present_ids: set[str], now: datetime):
        with self.tx() as con:
            for row in con.execute("SELECT reachout_id FROM reachouts WHERE row_missing=0").fetchall():
                if row["reachout_id"] not in present_ids:
                    con.execute("UPDATE reachouts SET row_missing=1 WHERE reachout_id=?", (row["reachout_id"],))
                    self._event(con, now, "QUEUE_ROW_MISSING", reachout_id=row["reachout_id"], outcome="WARNING",
                                details="The Reachout Queue row is gone; workflow state is kept. "
                                        "Add a row containing only this reachout_id to restore the status view.")

    def close(self, reachout_id: str, status: str, now: datetime) -> bool:
        """Cancel or resolve a delivered reachout. Returns False when nothing changed."""
        assert status in ("CANCELLED", "RESOLVED")
        with self.tx() as con:
            row = _dict(con.execute("SELECT * FROM reachouts WHERE reachout_id=?", (reachout_id,)).fetchone())
            if row["workflow_status"] != "OPEN":
                return False
            stamp = {"cancelled_at": iso(now)} if status == "CANCELLED" else {"resolved_at": iso(now)}
            fields = {"workflow_status": status, **stamp}
            if row["ack_status"] in ("PENDING",):
                fields["ack_status"] = "CLOSED"
            if row["written_status"] in ("PENDING",):
                fields["written_status"] = "CLOSED"
            self._update(con, "reachouts", "reachout_id", reachout_id, now, **fields)
            for obligation in con.execute(
                "SELECT obligation_id FROM reachout_obligations WHERE reachout_id=? AND status IN (?,?)",
                (reachout_id, *OPEN_OBLIGATION_STATES),
            ).fetchall():
                self._update(con, "reachout_obligations", "obligation_id", obligation["obligation_id"], now,
                             status="CANCELLED" if status == "CANCELLED" else "CLOSED_RESOLVED",
                             closed_at=iso(now), next_nudge_at=None)
                self._stale_nudges(con, obligation["obligation_id"], now)
            self._event(con, now, status, reachout_id=reachout_id, actor_role="OPERATOR")
            self._job(con, now, "update_original", reachout_id, rearm=True)
            self._job(con, now, "post_notice", reachout_id, dedupe_key=f"post_notice:{reachout_id}:{status}", target=status)
            return True

    # --- obligations -------------------------------------------------------------------

    def get_obligation(self, obligation_id: str) -> dict | None:
        return self.one("SELECT * FROM reachout_obligations WHERE obligation_id=?", (obligation_id,))

    def open_obligation(self, reachout_id: str, *, pending_only=True) -> dict | None:
        states = ("PENDING",) if pending_only else OPEN_OBLIGATION_STATES
        return self.one(
            f"SELECT * FROM reachout_obligations WHERE reachout_id=? AND status IN ({','.join('?' for _ in states)})",
            (reachout_id, *states),
        )

    def obligations_for(self, reachout_id: str) -> list[dict]:
        return self.all("SELECT * FROM reachout_obligations WHERE reachout_id=? ORDER BY created_at", (reachout_id,))

    def nudge_candidates(self, now: datetime) -> list[dict]:
        return self.all(
            "SELECT o.*, r.agent_id FROM reachout_obligations o JOIN reachouts r USING(reachout_id) "
            "WHERE o.status='PENDING' AND r.workflow_status='OPEN' AND o.due_at<=? AND o.next_nudge_at<=?",
            (iso(now), iso(now)),
        )

    def create_nudge(self, obligation_id: str, expected_next: str, lead_id: int, repeat_at: datetime | None,
                     now: datetime) -> str | None:
        with self.tx() as con:
            ob = con.execute("SELECT * FROM reachout_obligations WHERE obligation_id=?", (obligation_id,)).fetchone()
            if not ob or ob["status"] != "PENDING" or ob["next_nudge_at"] != expected_next:
                return None
            nudge_id = new_id("NDG", now)
            seq = ob["nudge_count"] + 1
            con.execute(
                "INSERT INTO reachout_nudges(nudge_id,obligation_id,reachout_id,seq,lead_discord_user_id,status,"
                "created_at,updated_at) VALUES(?,?,?,?,?,'QUEUED',?,?)",
                (nudge_id, obligation_id, ob["reachout_id"], seq, lead_id, iso(now), iso(now)),
            )
            self._update(con, "reachout_obligations", "obligation_id", obligation_id, now,
                         nudge_count=seq, next_nudge_at=iso(repeat_at))
            self._event(con, now, "LEAD_NUDGE_QUEUED", reachout_id=ob["reachout_id"], obligation_id=obligation_id,
                        actor=lead_id, details=f"nudge={nudge_id}; seq={seq}")
            self._job(con, now, "nudge_dm", nudge_id)
            return nudge_id

    def queue_auto_reminder(self, obligation_id: str, expected_next: str, repeat_at: datetime | None,
                            now: datetime) -> str | None:
        with self.tx() as con:
            ob = con.execute("SELECT * FROM reachout_obligations WHERE obligation_id=?", (obligation_id,)).fetchone()
            if not ob or ob["status"] != "PENDING" or ob["next_nudge_at"] != expected_next:
                return None
            seq = ob["nudge_count"] + 1
            event_id = self._event(con, now, "STANDARD_REMINDER", reachout_id=ob["reachout_id"],
                                   obligation_id=obligation_id, outcome="QUEUED", details="",
                                   dedupe_key=f"auto_reminder:{obligation_id}:{seq}")
            if not event_id:
                return None
            self._update(con, "reachout_obligations", "obligation_id", obligation_id, now,
                         nudge_count=seq, next_nudge_at=iso(repeat_at),
                         reminder_count=ob["reminder_count"] + 1, last_reminder_at=iso(now))
            self._job(con, now, "post_reminder", event_id)
            return event_id

    # --- responses ---------------------------------------------------------------------

    def get_response(self, response_id: str) -> dict | None:
        return self.one("SELECT * FROM reachout_responses WHERE response_id=?", (response_id,))

    def responses_for(self, reachout_id: str) -> list[dict]:
        return self.all("SELECT * FROM reachout_responses WHERE reachout_id=? ORDER BY submitted_at", (reachout_id,))

    def record_agent_submission(self, *, interaction_id: str, reachout_id: str, actor_id: int, actor_name: str,
                                expected_obligation_id: str | None, text: str, lead_id: int | None,
                                now: datetime, attachments: list[dict] = ()) -> tuple[dict, bool]:
        text = text.strip()
        with self.tx() as con:
            existing = _dict(con.execute("SELECT * FROM reachout_responses WHERE interaction_id=?",
                                         (interaction_id,)).fetchone())
            if existing:
                return existing, False
            row = _dict(con.execute("SELECT * FROM reachouts WHERE reachout_id=?", (reachout_id,)).fetchone())
            if not row or row["dispatch_state"] != "DELIVERED":
                raise WorkflowError("This reachout is not available.")
            if actor_id != row["agent_discord_user_id"]:
                raise PermissionError("Only the agent this reachout was sent to can respond.")
            if row["workflow_status"] == "CANCELLED":
                raise WorkflowError("This reachout was cancelled. Earlier messages and responses are kept.")
            pending = _dict(con.execute(
                "SELECT * FROM reachout_obligations WHERE reachout_id=? AND status='PENDING'", (reachout_id,),
            ).fetchone())
            if (pending["obligation_id"] if pending else None) != expected_obligation_id:
                raise StaleForm("This reachout changed while your form was open.")
            # An optional response has nothing to acknowledge, so it needs words.
            text_required = bool(pending["text_required"]) if pending else True
            if text_required and not text:
                raise ValueError("A written response is required.")
            acknowledgment = bool(pending and pending["obligation_type"] == ACKNOWLEDGMENT)
            if acknowledgment:
                response_type = "AGENT_ACKNOWLEDGMENT_WITH_RESPONSE" if text else "AGENT_ACKNOWLEDGMENT"
            else:
                response_type = "AGENT_RESPONSE"
            response_id = new_id("RSP", now)
            con.execute(
                "INSERT INTO reachout_responses(response_id,interaction_id,reachout_id,obligation_id,actor_discord_user_id,"
                "actor_name,actor_role,response_type,acknowledgment,text,attachments,submitted_at,review_status,"
                "lead_notify_status,updated_at) VALUES(?,?,?,?,?,?,'AGENT',?,?,?,?,?,'UNREVIEWED','QUEUED',?)",
                (response_id, interaction_id, reachout_id, pending["obligation_id"] if pending else None, actor_id,
                 actor_name, response_type, int(acknowledgment), text, json.dumps(list(attachments)), iso(now),
                 iso(now)),
            )
            fields = {"latest_agent_response_at": iso(now)}
            if pending:
                self._update(con, "reachout_obligations", "obligation_id", pending["obligation_id"], now,
                             status="SATISFIED", satisfied_by_response_id=response_id, satisfied_at=iso(now),
                             next_nudge_at=None)
                self._stale_nudges(con, pending["obligation_id"], now)
                if acknowledgment:
                    fields |= {"ack_status": "ACKNOWLEDGED", "ack_at": iso(now)}
                    if pending["text_required"]:
                        fields["written_status"] = "RECEIVED"
            self._update(con, "reachouts", "reachout_id", reachout_id, now, **fields)
            self._event(con, now, "AGENT_SUBMITTED", actor_role="AGENT", actor=actor_id, reachout_id=reachout_id,
                        response_id=response_id, obligation_id=pending["obligation_id"] if pending else None,
                        details=response_type)
            self._job(con, now, "post_response", response_id)
            if lead_id:
                self._job(con, now, "lead_dm", response_id, dedupe_key=f"lead_dm:{response_id}:{lead_id}", target=lead_id)
            else:
                self._update(con, "reachout_responses", "response_id", response_id, now,
                             lead_notify_status="FAILED_NO_LEAD")
            self._job(con, now, "update_original", reachout_id, rearm=True)
            return _dict(con.execute("SELECT * FROM reachout_responses WHERE response_id=?", (response_id,)).fetchone()), True

    def _lead_check(self, con, reachout: dict, lead_id: int):
        agent = con.execute("SELECT lead_discord_user_id FROM reachout_agents WHERE agent_id=?",
                            (reachout["agent_id"],)).fetchone()
        if not agent or agent["lead_discord_user_id"] != lead_id:
            raise PermissionError("You are not the currently assigned team lead for this agent.")

    def record_lead_reply(self, *, interaction_id: str, response_id: str, lead_id: int, lead_name: str, text: str,
                          requires_response: bool, now: datetime) -> tuple[dict, bool]:
        text = text.strip()
        if not text:
            raise ValueError("A reply is required.")
        with self.tx() as con:
            existing = _dict(con.execute("SELECT * FROM reachout_responses WHERE interaction_id=?",
                                         (interaction_id,)).fetchone())
            if existing:
                return existing, False
            agent_response = _dict(con.execute("SELECT * FROM reachout_responses WHERE response_id=?",
                                               (response_id,)).fetchone())
            if not agent_response or agent_response["actor_role"] != "AGENT":
                raise WorkflowError("That agent response could not be found.")
            row = _dict(con.execute("SELECT * FROM reachouts WHERE reachout_id=?",
                                    (agent_response["reachout_id"],)).fetchone())
            self._lead_check(con, row, lead_id)
            if row["workflow_status"] == "CANCELLED":
                raise WorkflowError("This reachout was cancelled; replies are closed. History is kept.")
            if requires_response and row["workflow_status"] != "OPEN":
                raise WorkflowError("This reachout is resolved. Create a new reachout to request another response.")
            reply_id = new_id("RSP", now)
            obligation_id = None
            if requires_response:
                for open_ob in con.execute(
                    "SELECT obligation_id FROM reachout_obligations WHERE reachout_id=? AND status IN (?,?)",
                    (row["reachout_id"], *OPEN_OBLIGATION_STATES),
                ).fetchall():
                    self._update(con, "reachout_obligations", "obligation_id", open_ob["obligation_id"], now,
                                 status="SUPERSEDED", superseded_by=reply_id, closed_at=iso(now), next_nudge_at=None)
                    self._stale_nudges(con, open_ob["obligation_id"], now)
                obligation_id = new_id("OBL", now)
                con.execute(
                    "INSERT INTO reachout_obligations(obligation_id,reachout_id,obligation_type,text_required,"
                    "requested_by_discord_user_id,requesting_response_id,required_agent_discord_user_id,status,"
                    "created_at,updated_at) VALUES(?,?,?,1,?,?,?,'AWAITING_DELIVERY',?,?)",
                    (obligation_id, row["reachout_id"], FOLLOW_UP, lead_id, reply_id, row["agent_discord_user_id"],
                     iso(now), iso(now)),
                )
            con.execute(
                "INSERT INTO reachout_responses(response_id,interaction_id,reachout_id,actor_discord_user_id,actor_name,"
                "actor_role,response_type,text,submitted_at,in_reply_to_response_id,created_obligation_id,"
                "review_status,updated_at) VALUES(?,?,?,?,?,'LEAD',?,?,?,?,?,'N/A',?)",
                (reply_id, interaction_id, row["reachout_id"], lead_id, lead_name,
                 "LEAD_REPLY_RESPONSE_REQUIRED" if requires_response else "LEAD_REPLY", text, iso(now),
                 response_id, obligation_id, iso(now)),
            )
            if agent_response["review_status"] == "UNREVIEWED":
                self._update(con, "reachout_responses", "response_id", response_id, now, review_status="REPLIED",
                             reviewed_by_discord_user_id=lead_id, reviewed_at=iso(now))
            self._event(con, now, "LEAD_REPLIED", actor_role="LEAD", actor=lead_id, reachout_id=row["reachout_id"],
                        response_id=reply_id, obligation_id=obligation_id,
                        details=f"in_reply_to={response_id}; response_required={requires_response}")
            self._job(con, now, "post_lead_reply", reply_id)
            self._job(con, now, "update_original", row["reachout_id"], rearm=True)
            return _dict(con.execute("SELECT * FROM reachout_responses WHERE response_id=?", (reply_id,)).fetchone()), True

    def acknowledge_response(self, *, response_id: str, lead_id: int, now: datetime) -> tuple[dict, bool]:
        with self.tx() as con:
            response = _dict(con.execute("SELECT * FROM reachout_responses WHERE response_id=?", (response_id,)).fetchone())
            if not response or response["actor_role"] != "AGENT":
                raise WorkflowError("That agent response could not be found.")
            row = _dict(con.execute("SELECT * FROM reachouts WHERE reachout_id=?", (response["reachout_id"],)).fetchone())
            self._lead_check(con, row, lead_id)
            if response["review_status"] == "ACKNOWLEDGED":
                return response, False
            self._update(con, "reachout_responses", "response_id", response_id, now, review_status="ACKNOWLEDGED",
                         reviewed_by_discord_user_id=lead_id, reviewed_at=iso(now))
            self._event(con, now, "LEAD_ACKNOWLEDGED", actor_role="LEAD", actor=lead_id,
                        reachout_id=row["reachout_id"], response_id=response_id, dedupe_key=f"lead_ack:{response_id}")
            self._job(con, now, "post_lead_ack", response_id)
            return _dict(con.execute("SELECT * FROM reachout_responses WHERE response_id=?", (response_id,)).fetchone()), True

    def update_response(self, response_id: str, now: datetime, **fields):
        with self.tx() as con:
            self._update(con, "reachout_responses", "response_id", response_id, now, **fields)

    def follow_up_posted(self, reply_id: str, *, message_id: int, delivered_at: datetime, due_at: datetime | None,
                         now: datetime):
        with self.tx() as con:
            reply = con.execute("SELECT * FROM reachout_responses WHERE response_id=?", (reply_id,)).fetchone()
            self._update(con, "reachout_responses", "response_id", reply_id, now, thread_message_id=message_id,
                         thread_post_status="POSTED", last_error=None)
            obligation_id = reply["created_obligation_id"]
            if obligation_id:
                ob = con.execute("SELECT status FROM reachout_obligations WHERE obligation_id=?", (obligation_id,)).fetchone()
                if ob and ob["status"] == "AWAITING_DELIVERY":
                    self._update(con, "reachout_obligations", "obligation_id", obligation_id, now, status="PENDING",
                                 delivered_at=iso(delivered_at), due_at=iso(due_at), next_nudge_at=iso(due_at))
            self._job(con, now, "update_original", reply["reachout_id"], rearm=True)

    # --- nudges and reminders ----------------------------------------------------------

    def get_nudge(self, nudge_id: str) -> dict | None:
        return self.one("SELECT * FROM reachout_nudges WHERE nudge_id=?", (nudge_id,))

    def update_nudge(self, nudge_id: str, now: datetime, **fields):
        with self.tx() as con:
            self._update(con, "reachout_nudges", "nudge_id", nudge_id, now, **fields)

    def _nudge_check(self, con, nudge_id: str, lead_id: int):
        nudge = _dict(con.execute("SELECT * FROM reachout_nudges WHERE nudge_id=?", (nudge_id,)).fetchone())
        if not nudge:
            raise WorkflowError("This reminder request could not be found.")
        row = _dict(con.execute("SELECT * FROM reachouts WHERE reachout_id=?", (nudge["reachout_id"],)).fetchone())
        self._lead_check(con, row, lead_id)
        if nudge["status"] == "HANDLED":
            raise WorkflowError(f"Already handled ({nudge['handled_action']}).")
        ob = _dict(con.execute("SELECT * FROM reachout_obligations WHERE obligation_id=?",
                               (nudge["obligation_id"],)).fetchone())
        if nudge["status"] == "STALE" or ob["status"] != "PENDING" or row["workflow_status"] != "OPEN":
            if nudge["status"] != "STALE":
                self._update(con, "reachout_nudges", "nudge_id", nudge_id, utcnow(), status="STALE")
            raise WorkflowError("No longer needed: the agent already responded, or the request was closed.")
        return nudge, ob, row

    def record_reminder(self, *, nudge_id: str, lead_id: int, custom_text: str | None, now: datetime) -> str:
        with self.tx() as con:
            nudge, ob, row = self._nudge_check(con, nudge_id, lead_id)
            kind = "CUSTOM_REMINDER" if custom_text else "STANDARD_REMINDER"
            event_id = self._event(
                con, now, kind, actor_role="LEAD", actor=lead_id, reachout_id=row["reachout_id"],
                obligation_id=ob["obligation_id"], outcome="QUEUED", details=(custom_text or "").strip(),
                dedupe_key=f"reminder:{nudge_id}",
            )
            self._update(con, "reachout_obligations", "obligation_id", ob["obligation_id"], now,
                         reminder_count=ob["reminder_count"] + 1, last_reminder_at=iso(now), last_reminder_by=lead_id)
            self._update(con, "reachout_nudges", "nudge_id", nudge_id, now, status="HANDLED", handled_action=kind,
                         handled_by_discord_user_id=lead_id, handled_at=iso(now))
            self._job(con, now, "post_reminder", event_id)
            self._job(con, now, "refresh_nudge_dm", nudge_id, rearm=True)
            return event_id

    def snooze(self, *, nudge_id: str, lead_id: int, until: datetime, label: str, now: datetime):
        with self.tx() as con:
            nudge, ob, row = self._nudge_check(con, nudge_id, lead_id)
            self._update(con, "reachout_obligations", "obligation_id", ob["obligation_id"], now, next_nudge_at=iso(until))
            self._update(con, "reachout_nudges", "nudge_id", nudge_id, now, status="HANDLED",
                         handled_action=f"SNOOZED ({label})", handled_by_discord_user_id=lead_id, handled_at=iso(now))
            self._event(con, now, "NUDGE_SNOOZED", actor_role="LEAD", actor=lead_id, reachout_id=row["reachout_id"],
                        obligation_id=ob["obligation_id"], details=f"until={iso(until)}")
            self._job(con, now, "refresh_nudge_dm", nudge_id, rearm=True)

    def get_event(self, event_id: str) -> dict | None:
        return self.one("SELECT * FROM reachout_events WHERE event_id=?", (event_id,))

    def update_event(self, event_id: str, now: datetime, **fields):
        with self.tx() as con:
            self._update(con, "reachout_events", "event_id", event_id, now, **fields)

    # --- jobs --------------------------------------------------------------------------

    def enqueue(self, kind: str, ref_id: str, now: datetime | None = None, **kwargs):
        with self.tx() as con:
            self._job(con, now or utcnow(), kind, ref_id, **kwargs)

    def claim_jobs(self, now: datetime, limit: int, lease_seconds: int = 300) -> list[dict]:
        claimed = []
        with self.tx() as con:
            for job in con.execute(
                "SELECT * FROM reachout_jobs WHERE status='PENDING' AND next_attempt_at<=? "
                "AND (lease_until IS NULL OR lease_until<?) ORDER BY id LIMIT ?",
                (iso(now), iso(now), limit),
            ).fetchall():
                con.execute("UPDATE reachout_jobs SET lease_until=? WHERE id=?",
                            (iso(now + timedelta(seconds=lease_seconds)), job["id"]))
                claimed.append(dict(job))
        return claimed

    def release_leases(self):
        """At startup: leases belong to a process that is gone (systemd's flock allows only one)."""
        with self.tx() as con:
            con.execute("UPDATE reachout_jobs SET lease_until=NULL WHERE status='PENDING'")

    def set_in_flight(self, job_id: int, value: bool):
        with self.tx() as con:
            con.execute("UPDATE reachout_jobs SET in_flight=? WHERE id=?", (int(value), job_id))

    def finish_job(self, job_id: int, now: datetime):
        with self.tx() as con:
            con.execute("UPDATE reachout_jobs SET status='DONE', in_flight=0, lease_until=NULL, last_error=NULL, "
                        "updated_at=? WHERE id=?", (iso(now), job_id))

    def retry_job(self, job_id: int, error: str, now: datetime) -> dict:
        with self.tx() as con:
            job = con.execute("SELECT attempts FROM reachout_jobs WHERE id=?", (job_id,)).fetchone()
            attempts = job["attempts"] + 1
            delay = min(60 * 2 ** (attempts - 1), 3600) * random.uniform(0.8, 1.2)
            con.execute(
                "UPDATE reachout_jobs SET attempts=?, next_attempt_at=?, lease_until=NULL, last_error=?, updated_at=? "
                "WHERE id=?", (attempts, iso(now + timedelta(seconds=delay)), error[:1000], iso(now), job_id),
            )
            return dict(con.execute("SELECT * FROM reachout_jobs WHERE id=?", (job_id,)).fetchone())

    def fail_job(self, job_id: int, error: str, now: datetime):
        with self.tx() as con:
            con.execute("UPDATE reachout_jobs SET status='FAILED', lease_until=NULL, last_error=?, updated_at=? "
                        "WHERE id=?", (error[:1000], iso(now), job_id))

    def mark_job_alerted(self, job_id: int):
        with self.tx() as con:
            con.execute("UPDATE reachout_jobs SET alerted=1 WHERE id=?", (job_id,))

    def rearm_failed_jobs(self, now: datetime) -> int:
        with self.tx() as con:
            return con.execute(
                "UPDATE reachout_jobs SET status='PENDING', attempts=0, next_attempt_at=?, updated_at=? "
                "WHERE status='FAILED'", (iso(now), iso(now)),
            ).rowcount

    def job_counts(self) -> dict:
        return {row["status"]: row["n"] for row in self.all("SELECT status, count(*) n FROM reachout_jobs GROUP BY status")}

    def pending_jobs_for(self, ref_id: str) -> list[dict]:
        return self.all("SELECT * FROM reachout_jobs WHERE ref_id=? AND status!='DONE'", (ref_id,))

    # --- Sheets mirror -----------------------------------------------------------------

    def dirty(self, name: str, *, everything=False, limit=500) -> list[dict]:
        table, key = SHEET_TABLES[name]
        where = "" if everything else "WHERE sheet_dirty=1"
        return self.all(f"SELECT * FROM {table} {where} ORDER BY updated_at LIMIT ?", (limit if not everything else -1,))

    def mark_clean(self, name: str, records: list[dict]):
        table, key = SHEET_TABLES[name]
        with self.tx() as con:
            for record in records:
                # Unchanged since it was read; a newer change stays dirty for the next sync.
                con.execute(f"UPDATE {table} SET sheet_dirty=0 WHERE {key}=? AND updated_at=?",
                            (record[key], record["updated_at"]))

    def summary(self, reachout_id: str) -> dict:
        responses = self.responses_for(reachout_id)
        agent = [r for r in responses if r["actor_role"] == "AGENT"]
        obligations = self.obligations_for(reachout_id)
        return {
            "agent_responses": len(agent),
            "acknowledged": sum(r["review_status"] == "ACKNOWLEDGED" for r in agent),
            "replied": sum(r["review_status"] == "REPLIED" for r in agent),
            "unreviewed": sum(r["review_status"] == "UNREVIEWED" for r in agent),
            "lead_replies": len(responses) - len(agent),
            "reminder_count": sum(o["reminder_count"] for o in obligations),
            "last_reminder_at": max((o["last_reminder_at"] for o in obligations if o["last_reminder_at"]), default=None),
            "open_obligation": next((o for o in obligations if o["status"] in OPEN_OBLIGATION_STATES), None),
        }

