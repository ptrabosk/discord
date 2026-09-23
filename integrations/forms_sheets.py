from __future__ import annotations

import os
import threading

import gspread
from gspread.utils import rowcol_to_a1

from forms.domain import sheet_tab_for

HEADERS = {
    "WIW Timesheet Adjustments": [
        "Submission ID", "Submitted At ET", "Submitted At UTC", "Discord User ID",
        "Employee Email", "Employee Name", "Status", "Adjustment Type", "Start Time ET",
        "Start Time UTC", "End Time ET", "End Time UTC", "Adjustment Reason", "Reviewed By",
        "Reviewed At ET", "Reviewed At UTC",
    ],
    "WIW Emergency Releases": [
        "Submission ID", "Submitted At ET", "Submitted At UTC", "Discord User ID",
        "Employee Email", "Employee Name", "Status", "Shift Start ET", "Shift Start UTC",
        "Release Reason", "Reviewed By", "Reviewed At ET", "Reviewed At UTC",
    ],
    "Questions Concerns Feedback": [
        "Submission ID", "Submitted At ET", "Submitted At UTC", "Discord User ID",
        "Employee Email", "Employee Name", "Type", "Category", "Message", "Status",
        "Reviewed By", "Reviewed At ET", "Reviewed At UTC",
    ],
}


def _reviewed_by(row):
    return row.get("reviewed_by_email") or row.get("reviewed_by_discord_id") or ""


def sheet_values(row: dict) -> list:
    tab = sheet_tab_for(row["form_type"], row.get("form_subtype"))
    common = [
        row["submission_id"], row["submitted_at_et"], row["submitted_at_utc"],
        str(row["discord_user_id"]), row["employee_email"], row["employee_name"],
    ]
    if tab == "WIW Timesheet Adjustments":
        return common + [
            row["status"], row.get("adjustment_type") or "", row.get("start_time_et") or "",
            row.get("start_time_utc") or "", row.get("end_time_et") or "",
            row.get("end_time_utc") or "", row["message"], _reviewed_by(row),
            row.get("reviewed_at_et") or "", row.get("reviewed_at_utc") or "",
        ]
    if tab == "WIW Emergency Releases":
        return common + [
            row["status"], row.get("start_time_et") or "", row.get("start_time_utc") or "",
            row["message"], _reviewed_by(row), row.get("reviewed_at_et") or "",
            row.get("reviewed_at_utc") or "",
        ]
    return common + [
        row["form_type"].title(), row.get("category") or "", row["message"], row["status"],
        _reviewed_by(row), row.get("reviewed_at_et") or "", row.get("reviewed_at_utc") or "",
    ]


class FormsSheet:
    def __init__(self, client):
        self.client = client
        self._lock = threading.Lock()

    def _spreadsheet(self):
        sheet_id = os.getenv("GOOGLE_FORMS_SHEET_ID") or os.environ["GOOGLE_SHEET_ID"]
        return self.client.open_by_key(sheet_id)

    def _worksheet(self, tab: str):
        spreadsheet = self._spreadsheet()
        try:
            worksheet = spreadsheet.worksheet(tab)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=tab, rows=100, cols=len(HEADERS[tab]))
            worksheet.update(range_name="A1", values=[HEADERS[tab]])
        actual_headers = worksheet.row_values(1)
        if actual_headers != HEADERS[tab]:
            raise RuntimeError(f"Worksheet {tab!r} does not have the required headers")
        return worksheet

    def sync_submission(self, row: dict):
        """Append or replace one row, using Submission ID as the idempotency key."""
        tab = sheet_tab_for(row["form_type"], row.get("form_subtype"))
        values = sheet_values(row)
        with self._lock:
            worksheet = self._worksheet(tab)
            ids = worksheet.col_values(1)
            try:
                row_number = ids.index(row["submission_id"]) + 1
            except ValueError:
                worksheet.append_row(values, value_input_option="RAW")
                return
            end = rowcol_to_a1(row_number, len(values))
            worksheet.update(
                range_name=f"A{row_number}:{end}",
                values=[values],
                value_input_option="RAW",
            )
