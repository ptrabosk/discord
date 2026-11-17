"""Reachout Google Sheets tabs: header-based reads, batched RAW writes, and template setup."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import gspread
from gspread.utils import rowcol_to_a1

from reachouts.domain import (
    CONFIG_KEYS,
    REACHOUT_TYPES,
    ReachoutConfig,
)

# Sheets allows a limited number of write requests per minute per user, and one poll makes several.
# A 429 means the request was refused, so repeating it after the window resets is safe.
QUOTA_ATTEMPTS = 3
QUOTA_WAIT_SECONDS = 65


def _retry_quota(call):
    for attempt in range(1, QUOTA_ATTEMPTS + 1):
        try:
            return call()
        except gspread.exceptions.APIError as exc:
            if exc.code != 429 or attempt == QUOTA_ATTEMPTS:
                raise
            time.sleep(QUOTA_WAIT_SECONDS)

QUEUE_TAB = "Reachout Queue"
AGENTS_TAB = "Reachout Agents"
RESPONSES_TAB = "Reachout Responses"
OBLIGATIONS_TAB = "Reachout Obligations"
EVENTS_TAB = "Reachout Events"
CONFIG_TAB = "Reachout Config"
# Older sheets had this tab; --recreate removes it.
RETIRED_TABS = ("Reachout Holidays",)

HISTORY_TAB = "Reachout History"
# The queue only holds what still has to be sent; operators delete rows once they say SENT.
QUEUE_OPERATOR = [
    "send", "agent_email", "reachout_type", "title", "message_body", "reference_urls", "due_at_override",
]
QUEUE_BOT = ["reachout_id", "processing_status", "status_detail"]
# Pre-checkbox queue tabs; reachout-sheet-setup moves them aside and copies unsent rows over.
LEGACY_QUEUE_MARKER = "dispatch_action"
AGENTS_OPERATOR = [
    "agent_email", "active", "team", "timezone_override",
    "fallback_channel_id", "existing_channel_id",
]
AGENTS_BOT = [
    "agent_name", "agent_discord_user_id", "lead_email", "lead_discord_user_id", "private_channel_id", "directory_status", "directory_error",
    "directory_synced_at",
]
RESPONSES = [
    "response_id", "reachout_id", "obligation_id", "agent_email", "actor_discord_user_id", "actor_name",
    "actor_role", "response_type", "acknowledgment", "text", "submitted_at", "in_reply_to_response_id",
    "created_obligation_id", "review_status", "reviewed_by_discord_user_id", "reviewed_at",
    "thread_message_id", "thread_post_status", "lead_notify_status", "lead_notified_discord_user_id",
    "last_error", "synced_at",
]
OBLIGATIONS = [
    "obligation_id", "reachout_id", "agent_email", "obligation_type", "text_required",
    "requested_by_discord_user_id", "requesting_response_id", "required_agent_discord_user_id",
    "status", "overdue", "created_at", "delivered_at", "due_at", "satisfied_by_response_id", "satisfied_at",
    "superseded_by", "closed_at", "next_nudge_at", "nudge_count", "reminder_count", "last_reminder_at",
    "last_reminder_by", "synced_at",
]
HISTORY = [
    "reachout_id", "sent_at", "agent_email", "agent_name", "reachout_type", "title", "message", "status", "due_at",
    "agent_reply_1", "needs_tl_reply", "tl_reply", "agent_reply_2", "later_replies", "images", "thread_url",
    "synced_at",
]
EVENTS = [
    "event_id", "reachout_id", "response_id", "obligation_id", "event_type", "actor_role",
    "actor_discord_user_id", "occurred_at", "outcome", "discord_channel_id", "discord_message_id",
    "details", "synced_at",
]
CONFIG = ["key", "value", "description", "bot_status"]

TAB_HEADERS = {
    QUEUE_TAB: QUEUE_OPERATOR + QUEUE_BOT,
    HISTORY_TAB: HISTORY,
    AGENTS_TAB: AGENTS_OPERATOR + AGENTS_BOT,
    RESPONSES_TAB: RESPONSES,
    OBLIGATIONS_TAB: OBLIGATIONS,
    EVENTS_TAB: EVENTS,
    CONFIG_TAB: CONFIG,
}
BOT_COLUMNS = {
    QUEUE_TAB: QUEUE_BOT,
    AGENTS_TAB: AGENTS_BOT,
    CONFIG_TAB: ["bot_status"],
}
BOT_ONLY_TABS = (HISTORY_TAB, RESPONSES_TAB, OBLIGATIONS_TAB, EVENTS_TAB)
# Detailed audit trail; Reachout History is the everyday view.
HIDDEN_TABS = (RESPONSES_TAB, OBLIGATIONS_TAB, EVENTS_TAB)
# Columns holding Discord snowflakes; plain-text format stops Sheets rounding them.
ID_COLUMNS = {
    AGENTS_TAB: ["lead_discord_user_id", "fallback_channel_id", "existing_channel_id", "agent_discord_user_id",
                 "private_channel_id"],
}
DROPDOWNS = {
    QUEUE_TAB: {
        "reachout_type": list(REACHOUT_TYPES.values()),
    },
    AGENTS_TAB: {"active": ["TRUE", "FALSE"]},
}
CHECKBOXES = {QUEUE_TAB: ["send"]}
PROTECTION_PREFIX = "Reachout bot-managed"


class SheetSchemaError(RuntimeError):
    pass


class Table:
    def __init__(self, tab: str, values: list[list[str]]):
        self.tab = tab
        self.headers = [str(h).strip() for h in values[0]] if values else []
        self.rows: list[tuple[int, dict]] = []
        for offset, raw in enumerate(values[1:]):
            padded = list(raw) + [""] * (len(self.headers) - len(raw))
            row = {h: str(v) for h, v in zip(self.headers, padded) if h}
            if any(value.strip() for value in row.values()):
                self.rows.append((offset + 2, row))

    def col(self, header: str) -> int:
        return self.headers.index(header) + 1

    def require(self, headers: list[str]):
        missing = [h for h in headers if h not in self.headers]
        if missing:
            raise SheetSchemaError(
                f"Tab {self.tab!r} is missing headers: {', '.join(missing)}. Run `python main.py reachout-sheet-setup`."
            )
        duplicates = sorted({h for h in self.headers if h and self.headers.count(h) > 1})
        if duplicates:
            raise SheetSchemaError(f"Tab {self.tab!r} has duplicate headers: {', '.join(duplicates)}.")


def _a1(tab: str, row: int, col: int) -> str:
    return f"'{tab}'!{rowcol_to_a1(row, col)}"


def cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


class ReachoutSheet:
    def __init__(self, client, sheet_id: str):
        self.client = client
        self.sheet_id = sheet_id
        self._spreadsheet = None
        self._lock = threading.Lock()

    def spreadsheet(self):
        if self._spreadsheet is None:
            self._spreadsheet = self.client.open_by_key(self.sheet_id)
        return self._spreadsheet

    def read(self, tabs: list[str]) -> dict[str, Table]:
        with self._lock:
            try:
                result = self.spreadsheet().values_batch_get(
                    [f"'{tab}'" for tab in tabs], params={"valueRenderOption": "FORMATTED_VALUE"}
                )
            except gspread.exceptions.APIError as exc:
                if "Unable to parse range" in str(exc):
                    raise SheetSchemaError(
                        "A reachout tab is missing. Run `python main.py reachout-sheet-setup`."
                    ) from exc
                raise
        ranges = result.get("valueRanges", [])
        return {tab: Table(tab, ranges[i].get("values", []) if i < len(ranges) else []) for i, tab in enumerate(tabs)}

    def read_rows(self, tab: str, row_numbers: list[int]) -> dict[int, list[str]]:
        if not row_numbers:
            return {}
        with self._lock:
            result = self.spreadsheet().values_batch_get(
                [f"'{tab}'!{n}:{n}" for n in row_numbers], params={"valueRenderOption": "FORMATTED_VALUE"}
            )
        ranges = result.get("valueRanges", [])
        return {n: (ranges[i].get("values") or [[]])[0] for i, n in enumerate(row_numbers)}

    def write(self, updates: list[tuple[str, int, int, str]]):
        """Write single cells as RAW values, so supplied text is never parsed as a formula."""
        if not updates:
            return
        with self._lock:
            for start in range(0, len(updates), 2000):
                chunk = updates[start:start + 2000]
                body = {
                    "valueInputOption": "RAW",
                    "data": [{"range": _a1(tab, row, col), "values": [[value]]} for tab, row, col, value in chunk],
                }
                _retry_quota(lambda: self.spreadsheet().values_batch_update(body))

    def write_by_key(self, tab: str, key: str, values: dict[str, dict[str, str]]):
        """Write cells to the rows whose key column holds each key, located just before writing.

        Operators delete rows by hand, so row numbers from an earlier read may have shifted.
        Keys no longer in the tab are skipped; a key on several rows is written to each.
        """
        if not values:
            return
        table = self.read([tab])[tab]
        rows = {}
        for row_number, row in table.rows:
            rows.setdefault(row.get(key, "").strip(), []).append(row_number)
        self.write([
            (tab, row_number, table.col(header), value)
            for item, cells in values.items() for row_number in rows.get(item, [])
            for header, value in cells.items() if header in table.headers
        ])

    def upsert(self, records: dict[str, tuple[str, list[dict]]]) -> None:
        """Update or append bot-only rows keyed by their ID column, touching only changed cells."""
        records = {tab: item for tab, item in records.items() if item[1]}
        if not records:
            return
        tables = self.read(list(records))
        updates = []
        appends = {}
        for tab, (key, rows) in records.items():
            table = tables[tab]
            table.require(TAB_HEADERS[tab])
            existing = {}
            for row_number, row in table.rows:
                existing.setdefault(row.get(key, ""), (row_number, row))
            for record in rows:
                found = existing.get(record[key])
                if found is None:
                    appends.setdefault(tab, []).append([record.get(h, "") for h in table.headers])
                    continue
                row_number, current = found
                changed = [
                    (tab, row_number, table.col(header), value) for header, value in record.items()
                    if header != "synced_at" and header in table.headers and current.get(header, "") != value
                ]
                if changed and "synced_at" in record:
                    changed.append((tab, row_number, table.col("synced_at"), record["synced_at"]))
                updates.extend(changed)
        self.write(updates)
        with self._lock:
            for tab, rows in appends.items():
                _retry_quota(lambda tab=tab, rows=rows: self.spreadsheet().values_append(
                    f"'{tab}'!A1",
                    params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                    body={"values": rows},
                ))

    # --- template -----------------------------------------------------------------------

    def delete_tabs(self) -> list[str]:
        """Remove every reachout tab so setup can build them fresh. Other tabs are untouched."""
        spreadsheet = self.spreadsheet()
        existing = {ws.title: ws for ws in spreadsheet.worksheets()}
        doomed = [existing[tab] for tab in (*TAB_HEADERS, *RETIRED_TABS) if tab in existing]
        if doomed and len(doomed) == len(existing):
            spreadsheet.add_worksheet(title="Sheet1", rows=10, cols=5)  # a spreadsheet needs one tab
        for ws in doomed:
            spreadsheet.del_worksheet(ws)
        return [f"Deleted tab {ws.title!r}." for ws in doomed]

    def setup(self, service_account_email: str, config: ReachoutConfig | None = None) -> list[str]:
        """Create or extend the reachout tabs. Never sends anything and never edits operator rows."""
        config = config or ReachoutConfig()
        report = []
        spreadsheet = self.spreadsheet()
        existing = {ws.title: ws for ws in spreadsheet.worksheets()}
        created = set()
        carried = []
        old_queue = existing.get(QUEUE_TAB)
        if old_queue is not None and LEGACY_QUEUE_MARKER in [h.strip() for h in old_queue.row_values(1)]:
            carried, line = _retire_legacy_queue(old_queue)
            del existing[QUEUE_TAB]
            report.append(line)
        for tab, headers in TAB_HEADERS.items():
            if tab not in existing:
                existing[tab] = spreadsheet.add_worksheet(title=tab, rows=200, cols=max(len(headers), 26))
                existing[tab].update(range_name="A1", values=[headers], value_input_option="RAW")
                created.add(tab)
                report.append(f"Created tab {tab!r}.")
                continue
            current = [h.strip() for h in existing[tab].row_values(1)]
            missing = [h for h in headers if h not in current]
            if missing:
                start = len(current) + 1
                if existing[tab].col_count < start + len(missing) - 1:
                    existing[tab].add_cols(start + len(missing) - 1 - existing[tab].col_count)
                existing[tab].update(range_name=rowcol_to_a1(1, start), values=[missing], value_input_option="RAW")
                report.append(f"Added headers to {tab!r}: {', '.join(missing)}.")
            else:
                report.append(f"Tab {tab!r} already has every header.")

        metadata = spreadsheet.fetch_sheet_metadata({"fields": "sheets(properties,protectedRanges)"})
        protected = {
            pr.get("description", "")
            for sheet in metadata["sheets"] for pr in sheet.get("protectedRanges", [])
        }
        requests = []
        for tab, ws in existing.items():
            if tab not in TAB_HEADERS:
                continue
            headers = [h.strip() for h in ws.row_values(1)]
            sheet_id = ws.id
            requests.append({"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }})
            requests.append(_format(sheet_id, 0, 1, 0, len(headers), {"textFormat": {"bold": True}}, "userEnteredFormat.textFormat.bold"))
            for header in ID_COLUMNS.get(tab, []):
                index = headers.index(header)
                requests.append(_format(sheet_id, 1, None, index, index + 1,
                                        {"numberFormat": {"type": "TEXT", "pattern": "@"}}, "userEnteredFormat.numberFormat"))
            if tab in HIDDEN_TABS:
                requests.append({"updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "hidden": True}, "fields": "hidden",
                }})
            for header in CHECKBOXES.get(tab, []):
                index = headers.index(header)
                requests.append({"setDataValidation": {
                    "range": _range(sheet_id, 1, None, index, index + 1),
                    "rule": {"condition": {"type": "BOOLEAN"}, "strict": True},
                }})
            for header, values in DROPDOWNS.get(tab, {}).items():
                index = headers.index(header)
                requests.append({"setDataValidation": {
                    "range": _range(sheet_id, 1, None, index, index + 1),
                    "rule": {
                        "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]},
                        "strict": True, "showCustomUi": True,
                    },
                }})
            bot_ranges = []
            if tab in BOT_ONLY_TABS:
                bot_ranges = [(0, len(headers), f"{PROTECTION_PREFIX}: {tab}")]
            for header in BOT_COLUMNS.get(tab, []):
                index = headers.index(header)
                bot_ranges.append((index, index + 1, f"{PROTECTION_PREFIX}: {tab} {header}"))
            for start, end, description in bot_ranges:
                requests.append(_format(sheet_id, 0, 1, start, end,
                                        {"backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85}},
                                        "userEnteredFormat.backgroundColor"))
                if description not in protected:
                    requests.append({"addProtectedRange": {"protectedRange": {
                        "range": _range(sheet_id, 0, None, start, end),
                        "description": description,
                        "warningOnly": False,
                        "editors": {"users": [service_account_email]},
                    }}})
            header_lock = f"{PROTECTION_PREFIX}: {tab} header row"
            if tab not in BOT_ONLY_TABS and header_lock not in protected:
                requests.append({"addProtectedRange": {"protectedRange": {
                    "range": _range(sheet_id, 0, 1, 0, len(headers)),
                    "description": header_lock,
                    "warningOnly": True,
                }}})
        spreadsheet.batch_update({"requests": requests})
        report.append("Applied header formatting, plain-text ID columns, checkboxes, dropdowns, protections, "
                      "and hid the detailed audit tabs.")
        if carried:
            existing[QUEUE_TAB].append_rows([_ordered(QUEUE_OPERATOR + QUEUE_BOT, row) for row in carried],
                                            value_input_option="RAW")
            report.append(f"Copied {len(carried)} unsent row(s) into the new {QUEUE_TAB!r} tab.")

        if CONFIG_TAB in created:
            defaults = {
                "timezone": config.timezone,
                "business_weekdays": "MON,TUE,WED,THU,FRI",
                "default_deadline_business_days": str(config.deadline_business_days),
                "nudge_repeat_business_days": str(config.nudge_repeat_business_days),
                "reminder_template": config.reminder_template,
                "acknowledgment_notice": config.acknowledgment_notice,
            }
            existing[CONFIG_TAB].append_rows(
                [[key, defaults[key], description, ""] for key, description in CONFIG_KEYS.items()],
                value_input_option="RAW",
            )
            report.append("Wrote default configuration rows.")
        if AGENTS_TAB in created:
            existing[AGENTS_TAB].append_rows([_ordered(AGENTS_OPERATOR + AGENTS_BOT, {
                "agent_email": "example.agent@example.invalid",
                "active": "FALSE",
                "team": "EXAMPLE",
            })], value_input_option="RAW")
            report.append("Added one example agent row (example.agent@example.invalid, inactive, never used).")
        if QUEUE_TAB in created and old_queue is None:
            existing[QUEUE_TAB].append_rows(
                [_ordered(QUEUE_OPERATOR + QUEUE_BOT, row) for row in SAMPLE_ROWS], value_input_option="RAW"
            )
            report.append("Added eight DRAFT sample rows (two per type, four with Discord formatting) for example.agent@example.invalid (they can never be sent).")
        return report


def _retire_legacy_queue(worksheet) -> tuple[list[dict], str]:
    """Rename a pre-checkbox queue tab to a dated backup and return its unsent rows in the new layout.

    READY becomes a ticked box and DRAFT an unticked one. Sent and cancelled rows stay only in the
    backup; their content and replies are in the database and the Reachout History tab.
    """
    values = worksheet.get_all_values()
    table = Table(QUEUE_TAB, values)
    carried = []
    for _, row in table.rows:
        status = (row.get("processing_status") or "").strip().upper()
        if status in ("DELIVERED", "SENDING", "CANCELLED", "CANCELLED_BEFORE_SEND"):
            continue
        new = {h: row.get(h, "") for h in QUEUE_OPERATOR + QUEUE_BOT if h != "send"}
        new["send"] = "TRUE" if (row.get("dispatch_action") or "").strip().upper() == "READY" else "FALSE"
        carried.append(new)
    backup = f"{QUEUE_TAB} (old {datetime.now(timezone.utc):%Y-%m-%d %H%M})"
    worksheet.update_title(backup)
    return carried, f"Renamed the old queue tab to {backup!r} as a backup."


def _ordered(headers: list[str], values: dict) -> list[str]:
    return [values.get(h, "") for h in headers]


def _range(sheet_id, row_start, row_end, col_start, col_end) -> dict:
    grid = {"sheetId": sheet_id, "startRowIndex": row_start, "startColumnIndex": col_start, "endColumnIndex": col_end}
    if row_end is not None:
        grid["endRowIndex"] = row_end
    return grid


def _format(sheet_id, row_start, row_end, col_start, col_end, fmt: dict, fields: str) -> dict:
    return {"repeatCell": {
        "range": _range(sheet_id, row_start, row_end, col_start, col_end),
        "cell": {"userEnteredFormat": fmt},
        "fields": fields,
    }}


def _sample(reachout_type, title, body, **extra):
    return {"send": "FALSE", "agent_email": "example.agent@example.invalid", "reachout_type": reachout_type,
            "title": f"EXAMPLE — {title}", "message_body": body} | extra


# Two per type; the second of each shows Discord formatting typed as plain characters in the cell.
SAMPLE_ROWS = [
    _sample("Weekly performance", "Week of Jan 5",
            "EXAMPLE ONLY. Strengths this week: ... Areas to focus on: ...",
            reference_urls="https://example.invalid/weekly-report"),
    _sample("Weekly performance", "Week of Jan 12 (formatted)",
            "EXAMPLE ONLY.\n**What went well**\n- Fast first responses\n- *Clear* closing messages\n\n"
            "**To work on**\n- __Check order history__ before replying\n\n> Keep it up!"),
    _sample("Agent behaviors", "Behavior follow-up",
            "EXAMPLE ONLY. Describe the observed behavior and the expectation here."),
    _sample("Agent behaviors", "Tone in chat (formatted)",
            "EXAMPLE ONLY.\n**What we saw:** a reply that came across as *dismissive*.\n"
            "**Expectation:** acknowledge the customer's concern first.\n~~Old macro~~ → use the new greeting macro."),
    _sample("Opt outs", "Opt-out handling",
            "EXAMPLE ONLY. Describe the opt-out case. A written explanation is required for this one."),
    _sample("Opt outs", "Missed STOP request (formatted)",
            "EXAMPLE ONLY.\nThe customer replied **STOP** and received another message.\n"
            "1. Confirm the opt-out was recorded\n2. *Do not* message the customer again\n\n> Opt-outs are a compliance requirement."),
    _sample("Flagged messages", "Flagged message review",
            "EXAMPLE ONLY. Quote or link the flagged message and explain the concern.",
            due_at_override="2099-01-07 17:00", reference_urls="https://example.invalid/ticket/1"),
    _sample("Flagged messages", "Flagged wording (formatted)",
            "EXAMPLE ONLY.\n**Flagged text:**\n> \"We can't help you with that.\"\n\n"
            "*Why it was flagged:* it ends the conversation without an alternative. __Offer a next step__ instead.",
            reference_urls="https://example.invalid/ticket/2"),
]


def service_account_email() -> str:
    with open(os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], encoding="utf-8") as handle:
        return json.load(handle)["client_email"]


def setup_cli() -> int:
    """`python main.py reachout-sheet-setup`: create the tabs. Never connects to Discord."""
    from integrations.google_sheets import AccessSheet

    sheet_id = os.getenv("REACHOUT_SHEET_ID", "").strip()
    if not sheet_id:
        print("Set REACHOUT_SHEET_ID to the reachout spreadsheet ID first.")
        return 2
    sheet = ReachoutSheet(AccessSheet().client, sheet_id)
    email = service_account_email()
    if "--recreate" in sys.argv:
        title = sheet.spreadsheet().title
        answer = input(f"Delete and rebuild every reachout tab in {title!r}? All rows in them are lost. Type DELETE: ")
        if answer.strip() != "DELETE":
            print("Cancelled; nothing changed.")
            return 1
        for line in sheet.delete_tabs():
            print(line)
    for line in sheet.setup(email):
        print(line)
    print(f"Done. Share the spreadsheet with {email} as Editor if you have not already.")
    return 0
