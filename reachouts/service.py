"""Reachout workflow: the manual Sheets poll, Discord delivery, team-lead DMs, and overdue nudges.

SQLite is authoritative for workflow state; Sheets is authoritative for operator input and is
otherwise a mirror. Discord side effects run as durable jobs (see ReachoutRepository._job).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord.ext import tasks

from database.database import get_verified_by_email, get_verified_user
from reachouts import views
from reachouts.domain import (
    ACKNOWLEDGMENT,
    MAX_REMINDER,
    REACHOUT_TYPES,
    DUE_AFTER_DAYS,
    WEEKLY,
    Payload,
    ReachoutConfig,
    add_business_days,
    directory_key,
    evaluate_agent_row,
    roster_lead_email,
    input_hash,
    iso,
    local_text,
    wants_send,
    parse_config,
    parse_iso,
    sheet_time,
    utcnow,
    validate_row,
    zone,
)
from reachouts.repository import ReachoutRepository, WorkflowError
from reachouts.sheets import (
    AGENTS_TAB,
    CONFIG_TAB,
    EVENTS,
    EVENTS_TAB,
    HISTORY_TAB,
    OBLIGATIONS,
    OBLIGATIONS_TAB,
    QUEUE_TAB,
    RESPONSES,
    RESPONSES_TAB,
    TAB_HEADERS,
    ReachoutSheet,
    cell,
)

log = logging.getLogger(__name__)
DEFAULT_ERROR_CHANNEL_ID = 1550994656966344714


ATTACHMENT_DIR = Path(__file__).resolve().parents[1] / "data" / "reachout_attachments"
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def check_images(images):
    if len(images) > MAX_IMAGES:
        raise ValueError(f"Attach at most {MAX_IMAGES} images.")
    for image in images:
        if not (image.content_type or "").startswith("image/"):
            raise ValueError(f"{image.filename} is not an image. Only images can be attached.")
        if image.size > MAX_IMAGE_BYTES:
            raise ValueError(f"{image.filename} is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")


async def save_images(images, folder: Path) -> list[dict]:
    saved = []
    if images:
        folder.mkdir(parents=True, exist_ok=True)
    for number, image in enumerate(images, 1):
        filename = f"{number}-{Path(image.filename).name}"
        path = folder / filename
        await image.save(path)
        saved.append({"filename": filename, "path": str(path), "content_type": image.content_type})
    return saved


def queue_status(dispatch_state: str) -> str:
    """The queue's one-word status: WAITING until sent, then SENT; ERROR means read status_detail."""
    if dispatch_state in ("QUEUED", "SENDING"):
        return "SENDING"
    if dispatch_state == "DELIVERED":
        return "SENT"
    if dispatch_state in ("INVALID", "BLOCKED", "HELD_CHANGED"):
        return "ERROR"
    if dispatch_state in ("CANCELLED", "CANCELLED_BEFORE_SEND"):
        return "CANCELLED"
    return "WAITING"


def reachouts_enabled() -> bool:
    return bool(os.getenv("REACHOUT_SHEET_ID", "").strip())


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    sheet_id: str
    poll_seconds: int
    batch: int
    category_id: int
    test_mode: bool
    test_allowed: frozenset[int]
    error_channel_id: int
    test_allowed_emails: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> "Settings":
        tokens = [x.strip().lower() for x in os.getenv("REACHOUT_TEST_ALLOWED_USER_IDS", "").split(",") if x.strip()]
        allowed = {int(x) for x in tokens if x.isdigit()}
        return cls(
            sheet_id=os.getenv("REACHOUT_SHEET_ID", "").strip(),
            poll_seconds=max(30, _int_env("REACHOUT_POLL_SECONDS", 60)),
            batch=max(1, min(_int_env("REACHOUT_BATCH_SIZE", 10), 50)),
            category_id=_int_env("REACHOUT_CHANNEL_CATEGORY_ID", 0),
            # Test mode is on unless explicitly turned off, and only in .env: a Sheets editor cannot disable it.
            test_mode=os.getenv("REACHOUT_TEST_MODE", "true").strip().lower() not in {"false", "0", "no", "off"},
            test_allowed=frozenset(allowed),
            test_allowed_emails=frozenset(x for x in tokens if "@" in x),
            error_channel_id=_int_env("REACHOUT_ERROR_CHANNEL_ID", _int_env("FORM_ERROR_CHANNEL_ID", DEFAULT_ERROR_CHANNEL_ID)),
        )


class Permanent(Exception):
    """A job that retrying cannot fix without someone changing something."""


class Blocked(Exception):
    """An environment problem (membership, permissions, audience) shown to operators in Sheets."""


class ReachoutService:
    def __init__(self, bot, access_sheet, *, repo: ReachoutRepository | None = None, sheet=None, settings=None):
        self.bot = bot
        self.access_sheet = access_sheet
        self.settings = settings or Settings.from_env()
        self.repo = repo or ReachoutRepository()
        self.sheet = sheet or ReachoutSheet(access_sheet.client, self.settings.sheet_id)
        self.config: ReachoutConfig = ReachoutConfig()
        self._poll_lock = asyncio.Lock()
        self._jobs_lock = asyncio.Lock()
        self._jobs_again = False
        self._history_written: dict[str, dict] = {}  # last History row written per reachout
        self._alerted: dict[str, datetime] = {}
        self._tasks: set[asyncio.Task] = set()

    def init(self):
        self.repo.init()
        self.repo.release_leases()
        self.config = self.repo.load_config()

    # --- helpers -----------------------------------------------------------------------

    def guild(self):
        return self.bot.get_guild(self.bot.guild_id)

    def agent_tz(self, agent: dict | None):
        return zone(agent["timezone"]) if agent and agent.get("timezone") else self.config.tz

    def test_blocked(self, user_id: int | None) -> bool:
        """In test mode, only allowlisted users (by Discord ID, or by verified personal email) are messaged."""
        if not self.settings.test_mode or user_id in self.settings.test_allowed:
            return False
        verified = get_verified_user(user_id) if user_id else None
        return not (verified and verified[1].lower() in self.settings.test_allowed_emails)

    def kick(self):
        """Run queued jobs soon without making the caller wait."""
        task = asyncio.get_running_loop().create_task(self.run_jobs())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def alert(self, kind: str, reference: str, error: Exception | None = None):
        """Operational alert to a private channel: kind and reference only, never content."""
        key = f"{kind}:{reference}"
        now = utcnow()
        if key in self._alerted and now - self._alerted[key] < timedelta(hours=1):
            return
        self._alerted[key] = now
        try:
            channel = self.bot.get_channel(self.settings.error_channel_id) or await self.bot.fetch_channel(
                self.settings.error_channel_id)
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.bot.guild_id:
                raise RuntimeError("error channel is not a text channel in the authorized server")
            if channel.permissions_for(channel.guild.default_role).view_channel:
                raise RuntimeError("error channel is visible to @everyone")
            detail = f" ({type(error).__name__})" if error else ""
            await channel.send(f"Reachout error: {kind}{detail}. Reference: {reference}. Check service logs.",
                               allowed_mentions=views.NO_MENTIONS)
        except Exception:
            log.exception("Could not send reachout alert %s", key)

    async def member(self, user_id: int | None):
        guild = self.guild()
        if not guild or not user_id:
            return None
        found = guild.get_member(user_id)
        if found:
            return found
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    def display_name(self, user_id: int | None, fallback: str = "Team lead") -> str:
        guild = self.guild()
        found = guild.get_member(user_id) if guild and user_id else None
        return found.display_name if found else fallback

    def agent_block_reason(self, agent: dict | None) -> str | None:
        if not agent:
            return "The agent is not in Reachout Agents."
        if agent["directory_status"] != "OK":
            return f"Reachout Agents row is {agent['directory_status']}: {agent['directory_error']}"
        if not agent["active"]:
            return "The agent is inactive."
        guild = self.guild()
        if guild and not guild.get_member(agent["agent_discord_user_id"]):
            return "The agent is not a member of the Discord server."
        if guild and not guild.get_member(agent["lead_discord_user_id"]):
            return "The assigned lead is not a member of the Discord server."
        if self.test_blocked(agent["agent_discord_user_id"]) or self.test_blocked(agent["lead_discord_user_id"]):
            return "Test mode: the agent and lead must both be in REACHOUT_TEST_ALLOWED_USER_IDS."
        if guild:
            channel_id = agent["private_channel_id"] or agent["existing_channel_id"]
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is None and not isinstance(guild.get_channel(self.settings.category_id), discord.CategoryChannel):
                return "No agent channel exists yet and REACHOUT_CHANNEL_CATEGORY_ID is not a category in this server."
            if isinstance(channel, discord.TextChannel):
                return self.audience_problem(channel, {agent["agent_discord_user_id"], agent["lead_discord_user_id"]})
        return None

    # --- polling -----------------------------------------------------------------------

    async def sync_sheets(self) -> dict:
        """What /reachout_sync runs: rewrite Sheets from SQLite. It never releases a ticked row and never runs a job."""
        now = utcnow()
        try:
            return await self.poll_once(full=True, send=False)
        except Exception as exc:
            log.exception("Reachout Sheets sync failed")
            self.repo.kv_set("last_poll_error", f"{iso(now)} {type(exc).__name__}: {exc}"[:1000])
            await self.alert("sheets_poll_failed", type(exc).__name__, exc)
            return {"poll_error": f"{type(exc).__name__}: {exc}"[:300]}

    async def tick(self, full: bool = True, *, send: bool = True) -> dict:
        """What /reachout_poll runs: the only Sheets read and write, then nudges and jobs.

        Sheets trouble never stops Discord jobs or nudges, or the reverse.
        """
        now = utcnow()
        stats: dict = {}
        try:
            stats = await self.poll_once(full=full, send=send)
        except Exception as exc:
            log.exception("Reachout Sheets poll failed")
            self.repo.kv_set("last_poll_error", f"{iso(now)} {type(exc).__name__}: {exc}"[:1000])
            await self.alert("sheets_poll_failed", type(exc).__name__, exc)
            stats["poll_error"] = f"{type(exc).__name__}: {exc}"[:300]
        stats["nudges_queued"] = await self.background_tick()
        return stats

    async def background_tick(self) -> int | None:
        """The automatic pass: overdue reminders and Discord job retries. It never touches Sheets."""
        queued = None
        try:
            queued = self.schedule_nudges(utcnow())
        except Exception as exc:
            log.exception("Reachout nudge scheduling failed")
            await self.alert("nudge_scheduling_failed", "scheduler", exc)
        await self.run_jobs()
        return queued

    async def poll_once(self, *, full: bool = False, send: bool = False) -> dict:
        async with self._poll_lock:
            now = utcnow()
            stats = Counter()
            tables = await asyncio.to_thread(self.sheet.read, [QUEUE_TAB, AGENTS_TAB, CONFIG_TAB])
            for tab, table in tables.items():
                table.require(TAB_HEADERS[tab])
            updates = self.apply_config(tables[CONFIG_TAB])
            updates += await self.sync_directory(tables[AGENTS_TAB], now)
            queue_updates = await self.process_queue(tables[QUEUE_TAB], now, stats, send=send)
            await asyncio.to_thread(self.sheet.write, updates)
            await asyncio.to_thread(self.sheet.write_by_key, QUEUE_TAB, "reachout_id", queue_updates)
            stats["cells_written"] = len(updates) + sum(len(cells) for cells in queue_updates.values())
            await self.sync_records(full=full)
            await self.sync_history(full=full)
            self.repo.kv_set("last_poll_ok", iso(now))
            return dict(stats)

    def diff(self, table, row_number: int, row: dict, values: dict, stamp: str | None = None) -> list:
        """Cell writes for values that differ; stamp is a timestamp column bumped only on change."""
        updates = [
            (table.tab, row_number, table.col(header), value)
            for header, value in values.items() if row.get(header, "") != value
        ]
        if updates and stamp:
            updates.append((table.tab, row_number, table.col(stamp), sheet_time(iso(utcnow()), self.config.tz)))
        return updates

    def apply_config(self, config_table) -> list:
        config, key_status, _ = parse_config([row for _, row in config_table.rows], [])
        if config != self.config:
            self.config = config
            self.repo.save_config(config)
        updates = []
        for row_number, row in config_table.rows:
            key = (row.get("key") or "").strip()
            if key:
                updates += self.diff(config_table, row_number, row, {"bot_status": key_status.get(key, "")})
        return updates

    async def roster(self) -> dict:
        """The Discord Access roster by email, read fresh on every /reachout_poll."""
        records = await asyncio.to_thread(self.access_sheet.all_records)
        return {str(r.get("Email", "")).strip().lower(): r for r in records}

    async def sync_directory(self, table, now: datetime) -> list:
        roster = await self.roster()
        counts = Counter(directory_key(row) for _, row in table.rows)
        guild = self.guild()
        updates, present = [], set()
        for row_number, row in table.rows:
            agent_id = directory_key(row)
            if not agent_id:
                continue
            present.add(agent_id)
            if counts[agent_id] > 1:
                message = "Duplicate agent_id: each agent needs exactly one row. Reachouts to this agent are blocked."
                if self.repo.get_agent(agent_id):
                    self.repo.upsert_agent(self.repo.get_agent(agent_id) | {
                        "directory_status": "INVALID", "directory_error": message}, now)
                updates += self.diff(table, row_number, row, {"directory_status": "INVALID", "directory_error": message})
                continue
            email = (row.get("agent_email") or "").strip().lower()
            verified = get_verified_by_email(email) if email else None
            record = roster.get(email)
            lead_email = roster_lead_email(record)
            lead = get_verified_by_email(lead_email) if lead_email else None
            values = evaluate_agent_row(row, record, verified[0] if verified else None, lead[0] if lead else None)
            if values["directory_status"] == "OK" and guild:
                if not guild.get_member(values["agent_discord_user_id"]):
                    values |= {"directory_status": "BLOCKED", "directory_error": "The agent is not in the Discord server."}
                elif not guild.get_member(values["lead_discord_user_id"]):
                    values |= {"directory_status": "BLOCKED", "directory_error": "The lead is not in the Discord server."}
            previous = self.repo.upsert_agent(values, now)
            stored = self.repo.get_agent(agent_id)
            if previous and previous["lead_discord_user_id"] and values["lead_discord_user_id"] \
                    and previous["lead_discord_user_id"] != values["lead_discord_user_id"]:
                self.repo.reassign_lead(agent_id, previous["lead_discord_user_id"], values["lead_discord_user_id"], now)
            elif previous and previous["active"] != stored["active"] and stored["private_channel_id"]:
                self.repo.enqueue("sync_channel", agent_id, now, rearm=True)
            updates += self.diff(table, row_number, row, {
                "agent_name": cell(stored["agent_name"]),
                "agent_discord_user_id": cell(stored["agent_discord_user_id"]),
                "lead_email": stored["lead_email"] or "",
                "lead_discord_user_id": cell(stored["lead_discord_user_id"]),
                "private_channel_id": cell(stored["private_channel_id"]),
                "directory_status": stored["directory_status"],
                "directory_error": stored["directory_error"] or "",
            }, stamp="directory_synced_at")
        for agent_id in self.repo.mark_agents_missing(present, now):
            agent = self.repo.get_agent(agent_id)
            if agent and agent["private_channel_id"]:
                self.repo.enqueue("sync_channel", agent_id, now, rearm=True)
        return updates

    async def process_queue(self, table, now: datetime, stats: Counter, *, send: bool = False) -> dict:
        """Release ticked rows (only when send is set) and return status cells keyed by reachout_id."""
        ids = Counter((row.get("reachout_id") or "").strip() for _, row in table.rows)
        updates = {}
        fresh = [(n, row) for n, row in table.rows
                 if send and not (row.get("reachout_id") or "").strip() and wants_send(row)]
        assigned = {}
        if fresh:
            current = await asyncio.to_thread(self.sheet.read_rows, QUEUE_TAB, [n for n, _ in fresh])
            id_col = table.col("reachout_id")
            for row_number, row in fresh:
                now_row = dict(zip(table.headers, list(current.get(row_number, [])) + [""] * len(table.headers)))
                # The row must still be the same ticked row without an ID: sorts move rows between reads.
                if (now_row.get("reachout_id") or "").strip() or input_hash(now_row) != input_hash(row) \
                        or not wants_send(now_row):
                    stats["id_assignment_deferred"] += 1
                    continue
                reachout_id = self.repo.assign_id(input_hash(row), now)
                await asyncio.to_thread(self.sheet.write, [(QUEUE_TAB, row_number, id_col, reachout_id)])
                assigned[row_number] = reachout_id
                ids[reachout_id] = 1
                stats["ids_assigned"] += 1
        present = set()
        for row_number, row in table.rows:
            reachout_id = (row.get("reachout_id") or "").strip() or assigned.get(row_number, "")
            if not reachout_id:
                continue
            present.add(reachout_id)
            rec = self.repo.get(reachout_id)
            if ids[reachout_id] > 1:
                values = {"processing_status": "ERROR",
                          "status_detail": f"{reachout_id} is on {ids[reachout_id]} rows. Delete the copied row; "
                                           "the bot ignores every copy until only one remains."}
                stats["duplicate_ids"] += 1
            elif not rec:
                values = {"processing_status": "ERROR",
                          "status_detail": "This reachout_id was not issued by the bot. Clear the reachout_id cell to "
                                           "release this row as a new reachout."}
                stats["unknown_ids"] += 1
            else:
                if send:
                    self.advance(rec, row, now, stats)
                values = self.queue_values(self.repo.get(reachout_id), now)
            changed = {h: v for h, v in values.items() if (row.get(h) or "") != v}
            if changed:
                updates[reachout_id] = changed
        self.repo.mark_rows_missing(present, now)
        return updates

    def advance(self, rec: dict, row: dict, now: datetime, stats: Counter):
        """Apply the operator's send checkbox to one known reachout."""
        reachout_id = rec["reachout_id"]
        action = "READY" if wants_send(row) else "DRAFT"
        current_hash = input_hash(row)
        self.repo.note_input(reachout_id, current_hash, now)
        state = rec["dispatch_state"]
        if state in ("DELIVERED", "SENDING", "CANCELLED"):
            return
        if action != "READY":
            if state != "WITHDRAWN":
                self.repo.withdraw(reachout_id, now)
            return
        held = ("The row changed after it was released and before delivery, so nothing was sent. Untick send, "
                "finish editing, then tick it again and run /reachout_poll.")
        if state == "QUEUED":
            if current_hash != rec["snapshot_hash"]:
                self.repo.set_dispatch(reachout_id, "HELD_CHANGED", held, now, event="HELD_CHANGED")
            return
        if state == "HELD_CHANGED":
            return
        if state == "ID_ASSIGNED" and current_hash != rec["assigned_hash"]:
            self.repo.set_dispatch(reachout_id, "HELD_CHANGED", held, now, event="HELD_CHANGED")
            return
        payload, errors = validate_row(row, self.config, now)
        if errors:
            self.repo.set_dispatch(reachout_id, "INVALID", " ".join(errors), now, event="VALIDATION_FAILED")
            stats["invalid"] += 1
            return
        agent = self.repo.find_agent(payload.agent_id)
        if not agent:
            self.repo.set_dispatch(reachout_id, "INVALID", f"agent_id {payload.agent_id} is not an agent_id or "
                                   "agent_email in Reachout Agents.", now, event="VALIDATION_FAILED")
            stats["invalid"] += 1
            return
        payload.agent_id = agent["agent_id"]  # the snapshot always holds the directory key
        reason = self.agent_block_reason(agent)
        if reason:
            self.repo.set_dispatch(reachout_id, "BLOCKED", reason + " Fix it, then run /reachout_poll again.", now, event="DISPATCH_BLOCKED")
            stats["blocked"] += 1
            return
        try:
            self.repo.queue(reachout_id, payload, current_hash, agent, now)
            stats["queued"] += 1
        except WorkflowError as exc:
            self.repo.set_dispatch(reachout_id, "INVALID", str(exc), now, event="VALIDATION_FAILED")
            stats["invalid"] += 1

    def queue_values(self, rec: dict, now: datetime) -> dict:
        detail = rec["status_detail"] or ""
        if rec["dispatch_state"] in ("QUEUED", "SENDING") and rec["last_error"]:
            detail = f"Retrying {sheet_time(rec['next_attempt_at'], self.config.tz) or 'soon'}: {rec['last_error']}"
        return {"processing_status": queue_status(rec["dispatch_state"]), "status_detail": detail}

    def history_row(self, rec: dict, now: datetime) -> dict:
        """One Reachout History row: the delivered content and the conversation in fixed columns."""
        tz = self.config.tz
        payload = Payload.from_json(rec["snapshot_json"])
        agent = self.repo.get_agent(rec["agent_id"]) or {}
        responses = self.repo.responses_for(rec["reachout_id"])
        replies = {"agent_reply_1": None, "tl_reply": None, "agent_reply_2": None}
        later = []
        for response in responses:
            slot = None
            if response["actor_role"] == "AGENT":
                if replies["agent_reply_1"] is None:
                    slot = "agent_reply_1"
                elif replies["tl_reply"] is not None and replies["agent_reply_2"] is None:
                    slot = "agent_reply_2"
            elif replies["agent_reply_1"] is not None and replies["tl_reply"] is None:
                slot = "tl_reply"
            if slot:
                replies[slot] = response
            else:
                later.append(response)

        def show(response):
            images = len(json.loads(response.get("attachments") or "[]"))
            text = response["text"] or "(acknowledged, no text)"
            if images:
                text += f" [{images} image(s)]"
            role = "agent" if response["actor_role"] == "AGENT" else "team lead"
            return f"{text}\n— {response['actor_name']} ({role}), {sheet_time(response['submitted_at'], tz)}"

        agent_responses = [r for r in responses if r["actor_role"] == "AGENT"]
        needs_tl = bool(agent_responses) and agent_responses[-1]["review_status"] == "UNREVIEWED"
        open_ob = self.repo.open_obligation(rec["reachout_id"])
        if rec["workflow_status"] in ("CANCELLED", "RESOLVED"):
            status = rec["workflow_status"]
        elif open_ob:
            status = "OVERDUE" if open_ob["due_at"] and open_ob["due_at"] <= iso(now) else "AWAITING_AGENT"
        elif needs_tl:
            status = "AWAITING_TL"
        else:
            status = "SENT" if payload.reachout_type == WEEKLY else "COMPLETE"
        return {
            "reachout_id": rec["reachout_id"],
            "sent_at": sheet_time(rec["delivered_at"], tz),
            "agent_email": payload.agent_id,
            "agent_name": agent.get("agent_name") or "",
            "reachout_type": REACHOUT_TYPES[payload.reachout_type],
            "title": payload.title,
            "message": payload.message_body,
            "status": status,
            "due_at": sheet_time(open_ob["due_at"] if open_ob else rec["due_at"], tz),
            **{slot: show(r) if r else "" for slot, r in replies.items()},
            "needs_tl_reply": cell(needs_tl),
            "later_replies": "\n\n".join(show(r) for r in later),
            "images": str(sum(len(json.loads(r.get("attachments") or "[]")) for r in agent_responses)),
            "thread_url": views.jump_url(self.bot.guild_id, rec["thread_id"]) or "",
        }

    async def sync_history(self, *, full: bool = False):
        """Upsert Reachout History rows that changed since the last write (all of them on a full pass)."""
        now = utcnow()
        rows = []
        for rec in self.repo.reachouts():
            if rec["dispatch_state"] != "DELIVERED":
                continue
            row = self.history_row(rec, now)
            if full or self._history_written.get(rec["reachout_id"]) != row:
                rows.append(row)
        if not rows:
            return
        stamp = sheet_time(iso(now), self.config.tz)
        await asyncio.to_thread(self.sheet.upsert, {HISTORY_TAB: ("reachout_id", [r | {"synced_at": stamp} for r in rows])})
        for row in rows:
            self._history_written[row["reachout_id"]] = row

    async def sync_records(self, *, full: bool = False):
        tz = self.config.tz
        stamp = sheet_time(iso(utcnow()), tz)
        agent_of = {r["reachout_id"]: r["agent_id"] or "" for r in self.repo.reachouts()}
        now = iso(utcnow())
        batches, read = {}, {}
        for name, tab, key, headers in (
            ("responses", RESPONSES_TAB, "response_id", RESPONSES),
            ("obligations", OBLIGATIONS_TAB, "obligation_id", OBLIGATIONS),
            ("events", EVENTS_TAB, "event_id", EVENTS),
        ):
            records = self.repo.dirty(name, everything=full)
            if not records:
                continue
            read[name] = records
            rows = []
            for record in records:
                values = {}
                for header in headers:
                    source = {"discord_channel_id": "channel_id", "discord_message_id": "message_id"}.get(header, header)
                    value = record.get(source)
                    if header.endswith("_at") and value:
                        value = sheet_time(value, tz)
                    elif header in ("acknowledgment", "text_required"):
                        value = cell(bool(value))
                    values[header] = cell(value)
                values["agent_email"] = agent_of.get(record.get("reachout_id"), "")
                if name == "obligations":
                    values["overdue"] = cell(record["status"] == "PENDING" and bool(record["due_at"]) and record["due_at"] <= now)
                values["synced_at"] = stamp
                rows.append({h: values.get(h, "") for h in headers})
            batches[tab] = (key, rows)
        if batches:
            await asyncio.to_thread(self.sheet.upsert, batches)
            for name, records in read.items():
                self.repo.mark_clean(name, records)

    # --- nudges ------------------------------------------------------------------------

    def schedule_nudges(self, now: datetime) -> int:
        """Overdue items get the standard reminder posted straight to the agent's thread."""
        queued = 0
        for ob in self.repo.nudge_candidates(now):
            agent = self.repo.get_agent(ob["agent_id"])
            if not agent or not agent["active"]:
                self.repo.add_event("NUDGE_SUPPRESSED", reachout_id=ob["reachout_id"], obligation_id=ob["obligation_id"],
                                    outcome="SKIPPED", details="Agent inactive; the reminder waits.",
                                    dedupe_key=f"nudge_suppressed:{ob['obligation_id']}:{ob['next_nudge_at']}", now=now)
                continue
            repeat = None
            if self.config.nudge_repeat_business_days:
                repeat = add_business_days(now, self.config.nudge_repeat_business_days, self.config, self.agent_tz(agent))
            if self.repo.queue_auto_reminder(ob["obligation_id"], ob["next_nudge_at"], repeat, now):
                queued += 1
        return queued

    # --- jobs --------------------------------------------------------------------------

    async def run_jobs(self):
        if self._jobs_lock.locked():
            self._jobs_again = True
            return
        async with self._jobs_lock:
            for _ in range(20):
                self._jobs_again = False
                jobs = self.repo.claim_jobs(utcnow(), self.settings.batch)
                for job in jobs:
                    await self.run_job(job)
                if not jobs and not self._jobs_again:
                    break

    async def run_job(self, job: dict):
        handler = getattr(self, f"job_{job['kind']}")
        try:
            await handler(job)
            self.repo.finish_job(job["id"], utcnow())
        except Permanent as exc:
            log.warning("Reachout job %s %s failed permanently: %s", job["kind"], job["ref_id"], exc)
            self.repo.fail_job(job["id"], str(exc), utcnow())
            await self.job_failed(job, str(exc), permanent=True)
            await self.alert(f"{job['kind']}_failed", job["ref_id"], exc)
        except Exception as exc:
            log.exception("Reachout job %s %s failed; will retry", job["kind"], job["ref_id"])
            retried = self.repo.retry_job(job["id"], f"{type(exc).__name__}: {exc}", utcnow())
            await self.job_failed(job, f"{type(exc).__name__}: {exc}", permanent=False,
                                  retry_at=parse_iso(retried["next_attempt_at"]))
            if retried["attempts"] >= 3 and not retried["alerted"]:
                self.repo.mark_job_alerted(job["id"])
                await self.alert(f"{job['kind']}_retrying", job["ref_id"], exc)

    async def job_failed(self, job: dict, error: str, *, permanent: bool, retry_at=None):
        now = utcnow()
        kind, ref = job["kind"], job["ref_id"]
        if kind == "dispatch":
            self.repo.record_dispatch_error(ref, ("Permanent: " if permanent else "") + error, retry_at, now)
        elif kind in ("post_response", "post_lead_reply"):
            self.repo.update_response(ref, now, last_error=error[:500],
                                      thread_post_status="FAILED" if permanent else "RETRYING")
        elif kind == "lead_dm":
            self.repo.update_response(ref, now, last_error=error[:500],
                                      lead_notify_status="FAILED" if permanent else "RETRYING")
            if permanent:
                self.repo.add_event("LEAD_NOTIFY_FAILED", response_id=ref, outcome="FAILED", details=error, now=now)
        elif kind == "nudge_dm" and permanent:
            nudge = self.repo.get_nudge(ref)
            self.repo.update_nudge(ref, now, status="FAILED")
            self.repo.add_event("LEAD_NUDGE_FAILED", reachout_id=nudge["reachout_id"], obligation_id=nudge["obligation_id"],
                                outcome="FAILED", details=error, now=now)
        elif kind == "post_reminder" and permanent:
            self.repo.update_event(ref, now, outcome="FAILED", details=error[:2000])

    async def send_once(self, job: dict, channel, marker, **kwargs) -> discord.Message:
        """Send unless an earlier attempt of this job may already have reached Discord.

        in_flight is committed before sending and cleared only when Discord definitely refused,
        so after a crash or timeout the next attempt searches recent history for the marker
        (the embed footer's ending, or a predicate over the embed) before posting again. This
        prevents blind duplicates but is not exactly-once: a matching message older than the
        scanned history would be missed.
        """
        if job.get("in_flight"):
            found = await self.find_marker(channel, marker)
            if found:
                return found
        self.repo.set_in_flight(job["id"], True)
        job["in_flight"] = 1
        try:
            return await channel.send(**kwargs)
        except discord.HTTPException as exc:
            if exc.status and 400 <= exc.status < 500 and exc.status != 429:
                self.repo.set_in_flight(job["id"], False)
                job["in_flight"] = 0
            raise

    async def find_marker(self, channel, marker):
        if isinstance(marker, str):
            text = marker

            def marker(message, embed):
                return bool(embed.footer and embed.footer.text and embed.footer.text.endswith(text))
        try:
            async for message in channel.history(limit=100):
                if message.author.id == self.bot.user.id and any(marker(message, embed) for embed in message.embeds):
                    return message
        except (discord.Forbidden, discord.NotFound):
            return None
        return None

    # channel and thread maintenance

    async def apply_overwrites(self, channel: discord.TextChannel, agent: dict):
        guild = channel.guild
        reason = f"Reachout channel for {agent['agent_id']}"

        async def merge(target, **fields):
            current = channel.overwrites_for(target)
            wanted = discord.PermissionOverwrite.from_pair(*current.pair())
            wanted.update(**fields)
            if wanted != current:
                await channel.set_permissions(target, overwrite=wanted, reason=reason)

        await merge(guild.default_role, view_channel=False)
        await merge(guild.me, view_channel=True, send_messages=True, send_messages_in_threads=True,
                    create_public_threads=True, manage_threads=True, embed_links=True, read_message_history=True)
        agent_member = await self.member(agent["agent_discord_user_id"])
        if agent_member:
            await merge(agent_member, view_channel=bool(agent["active"]), read_message_history=True,
                        send_messages=False, send_messages_in_threads=False,
                        create_public_threads=False, create_private_threads=False)
        lead_member = await self.member(agent["lead_discord_user_id"])
        if lead_member:
            await merge(lead_member, view_channel=True, read_message_history=True, send_messages=True,
                        send_messages_in_threads=True)
        old = agent["channel_lead_discord_user_id"]
        if old and old != agent["lead_discord_user_id"]:
            target = await self.member(old)
            if target is None:
                try:
                    target = await self.bot.fetch_user(old)
                except discord.NotFound:
                    target = None
            if target and target.id in {t.id for t in channel.overwrites}:
                await channel.set_permissions(target, overwrite=None, reason=f"{reason}: lead reassigned")
        self.repo.set_agent_channel(agent["agent_id"], channel.id, agent["lead_discord_user_id"], utcnow())

    def audience_problem(self, channel, allowed: set[int]) -> str | None:
        """Why a channel's viewers are broader than the allowed users, bot, and administrators."""
        guild = channel.guild
        if channel.permissions_for(guild.default_role).view_channel:
            return f"Channel {channel.id} is visible to @everyone."
        others = [m.id for m in channel.members
                  if m.id not in allowed and m.id != guild.me.id and not m.guild_permissions.administrator]
        if others:
            return f"Channel {channel.id} is visible to {len(others)} other member(s), e.g. Discord user {others[0]}."
        return None

    async def agent_channel(self, agent: dict) -> discord.TextChannel:
        guild = self.guild()
        if not guild:
            raise RuntimeError("Discord server is not available yet.")
        channel = None
        for channel_id in (agent["private_channel_id"], agent["existing_channel_id"]):
            if channel_id:
                channel = guild.get_channel(channel_id)
                if channel:
                    break
        if channel is None:
            category = guild.get_channel(self.settings.category_id)
            if not isinstance(category, discord.CategoryChannel):
                raise Blocked("REACHOUT_CHANNEL_CATEGORY_ID is not a category in this server.")
            # Named for the agent's first name; the bot tracks it by ID, so a duplicate name is harmless.
            label = agent["agent_first_name"] or agent["agent_id"]
            name = "reachout-" + (re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or agent["agent_id"].lower())[:80]
            # The bot's own allow goes in at creation; otherwise it could hide the channel from itself.
            channel = await guild.create_text_channel(
                name, category=category, reason=f"Reachout channel for {agent['agent_id']}",
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                                          read_message_history=True),
                },
            )
            if agent["private_channel_id"]:
                self.repo.add_event("AGENT_CHANNEL_RECREATED", channel_id=channel.id, outcome="WARNING",
                                    details=f"agent_id={agent['agent_id']}; old channel {agent['private_channel_id']} missing")
        if not isinstance(channel, discord.TextChannel):
            raise Blocked(f"Channel {channel.id} is not a text channel.")
        await self.apply_overwrites(channel, agent)
        problem = self.audience_problem(channel, {agent["agent_discord_user_id"], agent["lead_discord_user_id"]})
        if problem:
            raise Blocked(problem + " Nothing was posted.")
        return channel

    async def ensure_thread(self, rec: dict) -> discord.Thread:
        channel = self.bot.get_channel(rec["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(rec["channel_id"])
            except discord.NotFound:
                raise Permanent("The agent channel was deleted; responses stay saved in the database and Sheets.")
        try:
            message = await channel.fetch_message(rec["message_id"])
        except discord.NotFound:
            raise Permanent("The original reachout message was deleted; responses stay saved in the database and Sheets.")
        thread = channel.guild.get_thread(message.id)
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(message.id)
            except discord.NotFound:
                thread = None
        if thread is None:
            payload = Payload.from_json(rec["snapshot_json"])
            thread = await message.create_thread(
                name=views.clip(payload.title, 100),
                auto_archive_duration=10080, reason=f"Reachout {rec['reachout_id']}",
            )
        if thread.id != rec["thread_id"]:
            self.repo.set_thread(rec["reachout_id"], thread.id, utcnow(), recreated=rec["thread_id"] is not None)
        return thread

    async def thread_for(self, rec: dict) -> discord.Thread:
        thread = await self.ensure_thread(rec)
        problem = self.audience_problem(thread.parent, {rec["agent_discord_user_id"], self._lead_of(rec)})
        if problem:
            raise Blocked(problem)
        if thread.archived:
            await thread.edit(archived=False)
        return thread

    def _lead_of(self, rec: dict) -> int | None:
        agent = self.repo.get_agent(rec["agent_id"])
        return agent["lead_discord_user_id"] if agent else None

    async def deliver_to_lead(self, job: dict, agent: dict, lead_id: int, marker: str, **kwargs):
        """DM the lead; if DMs are closed, use the agent's fallback channel only if its audience is safe."""
        if self.test_blocked(lead_id):
            raise Permanent("Test mode: the lead is not in REACHOUT_TEST_ALLOWED_USER_IDS.")
        try:
            user = self.bot.get_user(lead_id) or await self.bot.fetch_user(lead_id)
            dm = user.dm_channel or await user.create_dm()
            return "DM_SENT", await self.send_once(job, dm, marker, allowed_mentions=views.NO_MENTIONS, **kwargs)
        except (discord.Forbidden, discord.NotFound) as exc:
            dm_error = f"DM failed ({type(exc).__name__})"
        guild = self.guild()
        fallback = guild.get_channel(agent["fallback_channel_id"]) if guild and agent["fallback_channel_id"] else None
        if fallback is None:
            raise Permanent(f"{dm_error}; no fallback_channel_id is configured for {agent['agent_id']}.")
        if not isinstance(fallback, discord.TextChannel):
            raise Permanent(f"{dm_error}; fallback channel {fallback.id} is not a text channel.")
        problem = self.audience_problem(fallback, {lead_id})
        if problem:
            raise Permanent(f"{dm_error}; fallback channel rejected: {problem}")
        return "FALLBACK_SENT", await self.send_once(
            job, fallback, marker, content=f"<@{lead_id}> The bot could not DM you, so this is posted here.",
            allowed_mentions=views.mention_only(lead_id), **kwargs,
        )

    def initial_due(self, payload: Payload, basis: datetime, agent: dict) -> tuple[datetime | None, str]:
        if not payload.ack_required:
            return None, "No acknowledgment required."
        default = basis + timedelta(days=DUE_AFTER_DAYS)
        if payload.due_at_override:
            override = parse_iso(payload.due_at_override)
            if override > basis:
                return override, "Due from due_at_override."
            return default, "due_at_override was not after delivery, so the default deadline applies."
        return default, f"Due {DUE_AFTER_DAYS} days after delivery."

    # job handlers

    async def job_dispatch(self, job: dict):
        now = utcnow()
        rec = self.repo.get(job["ref_id"])
        if rec["dispatch_state"] == "DELIVERED":
            if not rec["thread_id"] and rec["reachout_type"] != WEEKLY:
                await self.ensure_thread(rec)
            return
        if rec["dispatch_state"] not in ("QUEUED", "SENDING"):
            return
        agent = self.repo.get_agent(rec["agent_id"])
        reason = self.agent_block_reason(agent)
        if reason:
            self.repo.block_dispatch(rec["reachout_id"], reason + " Fix it, then run /reachout_poll again.", now)
            return
        try:
            channel = await self.agent_channel(agent)
        except Blocked as exc:
            self.repo.block_dispatch(rec["reachout_id"], str(exc), now)
            return
        rec = self.repo.mark_sending(rec["reachout_id"], now)
        if rec["dispatch_state"] != "SENDING":
            return
        payload = Payload.from_json(rec["snapshot_json"])
        basis = parse_iso(rec["prepared_at"])
        due, note = self.initial_due(payload, basis, agent)
        pending_preview = None
        if payload.ack_required:
            pending_preview = {"obligation_type": ACKNOWLEDGMENT, "text_required": int(payload.response_text_required),
                               "due_at": iso(due)}
        # The reachout card shows no ID, so a retry recognizes its own earlier post by content and time.
        def posted_earlier(message, embed):
            return (embed.title == views.clip(payload.title, 256) and embed.description == payload.message_body
                    and message.created_at >= basis)

        message = await self.send_once(
            job, channel, posted_earlier,
            content=f"<@{agent['agent_discord_user_id']}> You have a new reachout.",
            embed=views.reachout_embed(rec, payload, self.agent_tz(agent), pending_preview, due_preview=iso(due)),
            view=views.reachout_view(rec, pending_preview, self.bot.guild_id, False),
            allowed_mentions=views.mention_only(agent["agent_discord_user_id"]),
        )
        self.repo.mark_delivered(rec["reachout_id"], channel_id=channel.id, message_id=message.id,
                                 delivered_at=message.created_at, due_at=due, due_note=note, now=utcnow())
        if payload.reachout_type != WEEKLY:
            await self.ensure_thread(self.repo.get(rec["reachout_id"]))

    async def job_update_original(self, job: dict):
        rec = self.repo.get(job["ref_id"])
        if not rec or not rec["message_id"]:
            return
        agent = self.repo.get_agent(rec["agent_id"])
        payload = Payload.from_json(rec["snapshot_json"])
        pending = self.repo.open_obligation(rec["reachout_id"])
        has_responses = any(r["actor_role"] == "AGENT" for r in self.repo.responses_for(rec["reachout_id"]))
        message = self.bot.get_partial_messageable(rec["channel_id"]).get_partial_message(rec["message_id"])
        try:
            await message.edit(embed=views.reachout_embed(rec, payload, self.agent_tz(agent), pending),
                               view=views.reachout_view(rec, pending, self.bot.guild_id, has_responses))
        except discord.NotFound:
            self.repo.add_event("ORIGINAL_MESSAGE_MISSING", reachout_id=rec["reachout_id"], outcome="WARNING",
                                details="The reachout message was deleted in Discord; database state is unaffected.",
                                dedupe_key=f"ORIGINAL_MESSAGE_MISSING:{rec['reachout_id']}")

    async def job_post_response(self, job: dict):
        response = self.repo.get_response(job["ref_id"])
        if response["thread_message_id"]:
            return
        rec = self.repo.get(response["reachout_id"])
        thread = await self.thread_for(rec)
        agent = self.repo.get_agent(rec["agent_id"])
        files = [discord.File(item["path"], filename=item["filename"]) for item in json.loads(response["attachments"])
                 if Path(item["path"]).exists()]
        message = await self.send_once(job, thread, response["response_id"],
                                       embed=views.agent_post_embed(response, self.agent_tz(agent)),
                                       files=files, allowed_mentions=views.NO_MENTIONS)
        self.repo.update_response(response["response_id"], utcnow(), thread_message_id=message.id,
                                  thread_post_status="POSTED", last_error=None)

    async def job_lead_dm(self, job: dict):
        response = self.repo.get_response(job["ref_id"])
        rec = self.repo.get(response["reachout_id"])
        agent = self.repo.get_agent(rec["agent_id"])
        lead_id = int(job["target"])
        if agent["lead_discord_user_id"] != lead_id:
            return  # Reassigned since queuing; reassign_lead queued the new lead's copy.
        if response["lead_dm_message_id"] and response["lead_notified_discord_user_id"] == lead_id:
            return
        payload = Payload.from_json(rec["snapshot_json"])
        thread_url = views.jump_url(self.bot.guild_id, rec["thread_id"])
        status, message = await self.deliver_to_lead(
            job, agent, lead_id, response["response_id"],
            embed=views.lead_dm_embed(response, payload, agent, self.config.tz, self.bot.guild_id, rec),
            view=views.lead_dm_view(response, thread_url),
        )
        now = utcnow()
        self.repo.update_response(response["response_id"], now, lead_notify_status=status, last_error=None,
                                  lead_notified_discord_user_id=lead_id, lead_dm_channel_id=message.channel.id,
                                  lead_dm_message_id=message.id)
        self.repo.add_event("LEAD_NOTIFIED", reachout_id=rec["reachout_id"], response_id=response["response_id"],
                            actor=lead_id, channel_id=message.channel.id, message_id=message.id, details=status,
                            dedupe_key=f"lead_notified:{response['response_id']}:{lead_id}", now=now)

    async def job_post_lead_reply(self, job: dict):
        reply = self.repo.get_response(job["ref_id"])
        if reply["thread_message_id"]:
            return
        rec = self.repo.get(reply["reachout_id"])
        agent = self.repo.get_agent(rec["agent_id"])
        thread = await self.thread_for(rec)
        if not reply["prepared_at"]:
            self.repo.update_response(reply["response_id"], utcnow(), prepared_at=iso(utcnow()))
            reply = self.repo.get_response(reply["response_id"])
        required = reply["response_type"] == "LEAD_REPLY_RESPONSE_REQUIRED"
        due = parse_iso(reply["prepared_at"]) + timedelta(days=DUE_AFTER_DAYS) if required else None
        agent_id = rec["agent_discord_user_id"]
        message = await self.send_once(
            job, thread, reply["response_id"], content=f"<@{agent_id}>",
            embed=views.lead_post_embed(reply, self.agent_tz(agent), iso(due)),
            view=views.respond_view(rec["reachout_id"]) if required else discord.utils.MISSING,
            allowed_mentions=views.mention_only(agent_id),
        )
        self.repo.follow_up_posted(reply["response_id"], message_id=message.id, delivered_at=message.created_at,
                                   due_at=due, now=utcnow())

    async def job_post_lead_ack(self, job: dict):
        response = self.repo.get_response(job["ref_id"])
        if response["review_message_id"]:
            return
        rec = self.repo.get(response["reachout_id"])
        thread = await self.thread_for(rec)
        lead_name = self.display_name(response["reviewed_by_discord_user_id"])
        message = await self.send_once(job, thread, f"ack-{response['response_id']}",
                                       embed=views.lead_ack_embed(response, lead_name, self.config.tz),
                                       allowed_mentions=views.NO_MENTIONS)
        self.repo.update_response(response["response_id"], utcnow(), review_message_id=message.id)

    async def job_nudge_dm(self, job: dict):
        nudge = self.repo.get_nudge(job["ref_id"])
        if nudge["status"] != "QUEUED":
            return
        ob = self.repo.get_obligation(nudge["obligation_id"])
        rec = self.repo.get(nudge["reachout_id"])
        now = utcnow()
        if ob["status"] != "PENDING" or rec["workflow_status"] != "OPEN":
            self.repo.update_nudge(nudge["nudge_id"], now, status="STALE")
            return
        agent = self.repo.get_agent(rec["agent_id"])
        lead_id = agent["lead_discord_user_id"]
        if lead_id != nudge["lead_discord_user_id"]:
            self.repo.update_nudge(nudge["nudge_id"], now, lead_discord_user_id=lead_id)
            nudge = self.repo.get_nudge(nudge["nudge_id"])
        payload = Payload.from_json(rec["snapshot_json"])
        thread_url = views.jump_url(self.bot.guild_id, rec["thread_id"])
        status, message = await self.deliver_to_lead(
            job, agent, lead_id, nudge["nudge_id"],
            embed=views.nudge_embed(nudge, ob, payload, agent, rec, self.config.tz, self.bot.guild_id),
            view=views.nudge_view(nudge, thread_url),
        )
        self.repo.update_nudge(nudge["nudge_id"], utcnow(), status="SENT", channel_id=message.channel.id,
                               message_id=message.id)
        self.repo.add_event("LEAD_NUDGED", reachout_id=rec["reachout_id"], obligation_id=ob["obligation_id"],
                            actor=lead_id, channel_id=message.channel.id, message_id=message.id,
                            details=f"{status}; nudge={nudge['nudge_id']}", dedupe_key=f"nudged:{nudge['nudge_id']}")

    async def job_refresh_nudge_dm(self, job: dict):
        nudge = self.repo.get_nudge(job["ref_id"])
        if not nudge["message_id"]:
            return
        ob = self.repo.get_obligation(nudge["obligation_id"])
        rec = self.repo.get(nudge["reachout_id"])
        agent = self.repo.get_agent(rec["agent_id"])
        payload = Payload.from_json(rec["snapshot_json"])
        message = self.bot.get_partial_messageable(nudge["channel_id"]).get_partial_message(nudge["message_id"])
        try:
            await message.edit(embed=views.nudge_embed(nudge, ob, payload, agent, rec, self.config.tz, self.bot.guild_id),
                               view=views.nudge_view(nudge, views.jump_url(self.bot.guild_id, rec["thread_id"])))
        except (discord.NotFound, discord.Forbidden):
            pass  # The lead deleted the DM or closed DMs; the buttons reject stale actions anyway.

    async def job_post_reminder(self, job: dict):
        event = self.repo.get_event(job["ref_id"])
        if event["outcome"] != "QUEUED":
            return
        ob = self.repo.get_obligation(event["obligation_id"])
        rec = self.repo.get(event["reachout_id"])
        now = utcnow()
        if ob["status"] != "PENDING" or rec["workflow_status"] != "OPEN":
            self.repo.update_event(event["event_id"], now, outcome="SKIPPED_NO_LONGER_PENDING")
            return
        agent = self.repo.get_agent(rec["agent_id"])
        tz = self.agent_tz(agent)
        payload = Payload.from_json(rec["snapshot_json"])
        if event["event_type"] == "CUSTOM_REMINDER":
            text = event["details"]
        else:
            text = self.config.reminder_template.format(
                requirement=views.requirement(ob), title=payload.title, due_at=local_text(ob["due_at"], tz),
            )
        thread = await self.thread_for(rec)
        agent_id = rec["agent_discord_user_id"]
        message = await self.send_once(
            job, thread, event["event_id"], content=f"<@{agent_id}>",
            embed=views.reminder_embed(event, self.display_name(event["actor_discord_user_id"]), views.clip(text, 4000), tz),
            view=views.respond_view(rec["reachout_id"]), allowed_mentions=views.mention_only(agent_id),
        )
        self.repo.update_event(event["event_id"], utcnow(), outcome="POSTED", channel_id=thread.id, message_id=message.id)

    async def job_post_notice(self, job: dict):
        rec = self.repo.get(job["ref_id"])
        if not rec["message_id"]:
            return
        thread = await self.thread_for(rec)
        await self.send_once(job, thread, f"{job['target'].lower()}-{rec['reachout_id']}",
                             embed=views.notice_embed(rec, job["target"], self.config.tz),
                             allowed_mentions=views.NO_MENTIONS)

    async def job_sync_channel(self, job: dict):
        agent = self.repo.get_agent(job["ref_id"])
        guild = self.guild()
        channel = guild.get_channel(agent["private_channel_id"]) if guild and agent and agent["private_channel_id"] else None
        if isinstance(channel, discord.TextChannel):
            await self.apply_overwrites(channel, agent)

    # --- interactions ------------------------------------------------------------------

    def agent_form(self, reachout_id: str, user_id: int) -> dict:
        rec = self.repo.get(reachout_id)
        if not rec or rec["dispatch_state"] != "DELIVERED":
            raise WorkflowError("This reachout is not available.")
        if user_id != rec["agent_discord_user_id"]:
            raise PermissionError("Only the agent this reachout was sent to can respond.")
        if rec["workflow_status"] == "CANCELLED":
            raise WorkflowError("This reachout was cancelled. Earlier messages and responses are kept.")
        payload = Payload.from_json(rec["snapshot_json"])
        pending = self.repo.open_obligation(reachout_id)
        heading = f"**{REACHOUT_TYPES[payload.reachout_type]}: {views.clip(payload.title, 200)}**\n\n"
        if pending and pending["obligation_type"] == ACKNOWLEDGMENT:
            required = bool(pending["text_required"])
            return {
                "title": "Acknowledge & respond", "obligation_id": pending["obligation_id"], "text_required": required,
                "notice": heading + self.config.acknowledgment_notice
                          + ("\n\nA written explanation is required." if required else "\n\nA written response is optional."),
                "label": "Written response" + (" (required)" if required else " (optional)"),
                "description": "Records receipt/review, not necessarily agreement.",
            }
        if pending:
            return {
                "title": "Respond to follow-up", "obligation_id": pending["obligation_id"], "text_required": True,
                "notice": heading + "Your team lead asked for a written response.",
                "label": "Written response (required)", "description": "Posted in the reachout thread under your name.",
            }
        has_responses = any(r["actor_role"] == "AGENT" for r in self.repo.responses_for(reachout_id))
        title = "Respond" if payload.reachout_type == WEEKLY and not has_responses else "Add response"
        return {
            "title": title, "obligation_id": None, "text_required": True,
            "notice": heading + "This response is optional. Your team lead will be notified.",
            "label": "Your response", "description": "Posted in the reachout thread under your name.",
        }

    async def submit_agent(self, interaction, reachout_id: str, obligation_id: str | None, text: str,
                           images=()) -> dict:
        if interaction.guild_id != self.bot.guild_id:
            raise PermissionError("Reachouts can only be answered in the authorized server.")
        check_images(images)
        rec = self.repo.get(reachout_id)
        agent = self.repo.get_agent(rec["agent_id"]) if rec else None
        # Discord's upload links expire, so images are kept locally until the thread post is made.
        folder = ATTACHMENT_DIR / str(interaction.id)
        saved = await save_images(images, folder)
        try:
            response, created = self.repo.record_agent_submission(
                interaction_id=str(interaction.id), reachout_id=reachout_id, actor_id=interaction.user.id,
                actor_name=(agent or {}).get("agent_name") or interaction.user.display_name,
                expected_obligation_id=obligation_id, text=text,
                lead_id=agent["lead_discord_user_id"] if agent else None, now=utcnow(), attachments=saved,
            )
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        if created:
            self.kick()
        return response

    def lead_context(self, response_id: str, user_id: int, requires_response: bool) -> dict:
        response = self.repo.get_response(response_id)
        if not response or response["actor_role"] != "AGENT":
            raise WorkflowError("That agent response could not be found.")
        rec = self.repo.get(response["reachout_id"])
        if self._lead_of(rec) != user_id:
            raise PermissionError("You are not the currently assigned team lead for this agent.")
        if rec["workflow_status"] == "CANCELLED":
            raise WorkflowError("This reachout was cancelled; replies are closed. History is kept.")
        if requires_response and rec["workflow_status"] != "OPEN":
            raise WorkflowError("This reachout is resolved. Create a new reachout to request another response.")
        return response

    async def submit_lead_reply(self, interaction, response_id: str, requires_response: bool, text: str) -> dict:
        reply, created = self.repo.record_lead_reply(
            interaction_id=str(interaction.id), response_id=response_id, lead_id=interaction.user.id,
            lead_name=self.display_name(interaction.user.id, interaction.user.display_name), text=text,
            requires_response=requires_response, now=utcnow(),
        )
        if created:
            self.kick()
        return reply

    async def lead_acknowledge(self, interaction, response_id: str):
        result = self.repo.acknowledge_response(response_id=response_id, lead_id=interaction.user.id, now=utcnow())
        if result[1]:
            self.kick()
        return result

    async def refresh_lead_dm(self, message, response_id: str):
        if message is None:
            return
        response = self.repo.get_response(response_id)
        rec = self.repo.get(response["reachout_id"])
        agent = self.repo.get_agent(rec["agent_id"])
        try:
            await message.edit(
                embed=views.lead_dm_embed(response, Payload.from_json(rec["snapshot_json"]), agent, self.config.tz,
                                          self.bot.guild_id, rec),
                view=views.lead_dm_view(response, views.jump_url(self.bot.guild_id, rec["thread_id"])),
            )
        except discord.HTTPException:
            log.warning("Could not refresh lead DM for %s", response_id)

    def nudge_context(self, nudge_id: str, user_id: int) -> dict:
        nudge = self.repo.get_nudge(nudge_id)
        if not nudge:
            raise WorkflowError("This reminder request could not be found.")
        rec = self.repo.get(nudge["reachout_id"])
        if self._lead_of(rec) != user_id:
            raise PermissionError("You are not the currently assigned team lead for this agent.")
        ob = self.repo.get_obligation(nudge["obligation_id"])
        if nudge["status"] in ("HANDLED", "STALE") or ob["status"] != "PENDING" or rec["workflow_status"] != "OPEN":
            raise WorkflowError("No longer needed: it was already handled, the agent responded, or the request closed.")
        return nudge

    async def remind(self, interaction, nudge_id: str, custom_text: str | None):
        if custom_text is not None and not custom_text.strip():
            raise ValueError("A reminder message is required.")
        if custom_text and len(custom_text) > MAX_REMINDER:
            raise ValueError(f"Keep reminders under {MAX_REMINDER} characters.")
        self.repo.record_reminder(nudge_id=nudge_id, lead_id=interaction.user.id, custom_text=custom_text, now=utcnow())
        self.kick()

    async def snooze(self, interaction, nudge_id: str, option: str) -> datetime:
        now = utcnow()
        nudge = self.repo.get_nudge(nudge_id)
        if not nudge or option not in views.SNOOZE_OPTIONS:
            raise WorkflowError("That snooze option is not available.")
        agent = self.repo.get_agent(self.repo.get(nudge["reachout_id"])["agent_id"])
        if option.endswith("bd"):
            until = add_business_days(now, int(option[0]), self.config, self.agent_tz(agent))
        else:
            until = now + timedelta(hours=int(option[:-1]))
        self.repo.snooze(nudge_id=nudge_id, lead_id=interaction.user.id, until=until,
                         label=views.SNOOZE_OPTIONS[option], now=now)
        self.kick()
        return until

    def status_text(self, reachout_id: str) -> str:
        rec = self.repo.get(reachout_id)
        if not rec:
            return "No reachout with that ID."
        tz = self.config.tz
        summary = self.repo.summary(reachout_id)
        ob = summary["open_obligation"]
        jobs = self.repo.pending_jobs_for(reachout_id)
        return "\n".join([
            f"**{reachout_id}** — dispatch {rec['dispatch_state']}, workflow {rec['workflow_status']}",
            f"Detail: {rec['status_detail'] or '—'}",
            f"Delivered: {local_text(rec['delivered_at'], tz) or '—'}; due: {local_text(rec['due_at'], tz) or '—'}",
            f"Acknowledgment: {rec['ack_status'] or '—'}; written: {rec['written_status'] or '—'}",
            f"Open obligation: {ob['obligation_id'] + ' ' + ob['status'] if ob else 'none'}",
            f"Agent responses: {summary['agent_responses']} ({summary['unreviewed']} unreviewed); reminders: {summary['reminder_count']}",
            f"Unfinished jobs for this reachout: {len(jobs)}; last error: {rec['last_error'] or '—'}",
            f"Thread: {views.jump_url(self.bot.guild_id, rec['thread_id']) or '—'}",
        ])


class ReachoutLoop:
    def __init__(self, bot, service: ReachoutService):
        self.bot = bot
        self.service = service
        self.loop.change_interval(seconds=service.settings.poll_seconds)

    @tasks.loop(seconds=60)
    async def loop(self):
        try:
            await self.service.background_tick()
        except Exception as exc:
            log.exception("Reachout loop failed")
            await self.service.alert("reachout_loop_failed", "loop", exc)

    @loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
