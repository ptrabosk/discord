from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "forms.json"
DEFAULT_PROMPTS = {
    "form_type": "Choose a form type.",
    "when_i_work": "Choose a When I Work form.",
    "category": "Choose a category.",
    "adjustment_type": "Choose the adjustment type.",
    "start_date_range": "Choose the shift date range.",
    "start_date": "Choose the start date.",
    "start_hour": "Choose the start time.",
    "end_hour": "Choose the end hour.",
    "start_minute": "Choose the start minutes.",
    "end_minute": "Choose the end minutes.",
}


def load_form_config(path: Path = CONFIG_PATH) -> dict:
    """Load editable copy and choices while keeping stored form IDs stable."""
    config = json.loads(path.read_text(encoding="utf-8"))
    expected_names = {"when_i_work", "question", "concern", "feedback"}
    expected_wiw = {"timesheet_adjustment", "emergency_shift_release"}
    if set(config["form_names"]) != expected_names:
        raise ValueError("forms.json form_names must keep the four existing IDs")
    if set(config["when_i_work_names"]) != expected_wiw:
        raise ValueError("forms.json when_i_work_names must keep the two existing IDs")
    if set(config["message_fields"]) != (expected_names - {"when_i_work"}) | expected_wiw:
        raise ValueError("forms.json message_fields must keep the five existing IDs")
    if not set(config["prompts"]) <= set(DEFAULT_PROMPTS) or any(
        not isinstance(value, str) or not 1 <= len(value) <= 500
        for value in config["prompts"].values()
    ):
        raise ValueError("forms.json prompts must use known keys and be 1–500 characters")
    config["prompts"] = DEFAULT_PROMPTS | config["prompts"]
    for key in ("categories", "adjustment_types"):
        options = config[key]
        if not isinstance(options, list) or not 1 <= len(options) <= 25:
            raise ValueError(f"forms.json {key} must have 1–25 answers")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 100 for item in options):
            raise ValueError(f"forms.json {key} answers must be unique nonempty text under 100 characters")
        if len(options) != len(set(options)):
            raise ValueError(f"forms.json {key} answers must be unique")
    start_hours = config["start_hours"]
    if (
        not isinstance(start_hours, list)
        or not 1 <= len(start_hours) <= 25
        or any(type(hour) is not int or not 0 <= hour <= 23 for hour in start_hours)
        or len(start_hours) != len(set(start_hours))
    ):
        raise ValueError("forms.json start_hours must be 1–25 unique hours from 0 to 23")
    for names in (config["form_names"], config["when_i_work_names"]):
        if any(not isinstance(label, str) or not 1 <= len(label) <= 100 for label in names.values()):
            raise ValueError("forms.json choice labels must be 1–100 characters")
    for fields in config["message_fields"].values():
        for key, maximum in (("title", 45), ("question", 45), ("placeholder", 100)):
            value = fields[key]
            if not isinstance(value, str) or not 1 <= len(value) <= maximum:
                raise ValueError(f"forms.json {key} must be 1–{maximum} characters")
    return config


FORM_CONFIG = load_form_config()
CATEGORIES = tuple(FORM_CONFIG["categories"])
ADJUSTMENT_TYPES = tuple(FORM_CONFIG["adjustment_types"])
START_HOURS = tuple(FORM_CONFIG["start_hours"])

FORM_TABS = {
    ("when_i_work", "timesheet_adjustment"): "WIW Timesheet Adjustments",
    ("when_i_work", "emergency_shift_release"): "WIW Emergency Releases",
    ("question", None): "Questions Concerns Feedback",
    ("concern", None): "Questions Concerns Feedback",
    ("feedback", None): "Questions Concerns Feedback",
}

ACTION_STATUSES = {
    ("when_i_work", "timesheet_adjustment"): {
        "approve": "Approved",
        "reject": "Rejected",
        "need_info": "Needs Information",
    },
    ("when_i_work", "emergency_shift_release"): {
        "approve": "Approved",
        "reject": "Rejected",
        "need_info": "Needs Information",
    },
    ("question", None): {
        "answered": "Answered",
        "need_info": "Needs Information",
        "close": "Closed",
    },
    ("concern", None): {
        "reviewed": "Reviewed",
        "need_info": "Needs Information",
        "close": "Closed",
    },
    ("feedback", None): {
        "reviewed": "Reviewed",
        "follow_up": "Follow-up Required",
        "close": "Closed",
    },
}

FINAL_STATUSES = {"Approved", "Rejected", "Answered", "Reviewed", "Closed"}
VALID_STATUSES = FINAL_STATUSES | {"Pending", "Needs Information", "Follow-up Required"}


@dataclass
class FormState:
    form_type: str
    form_subtype: str | None = None
    category: str | None = None
    adjustment_type: str | None = None
    start_date: date | None = None
    start_hour: int | None = None
    start_minute: int | None = None
    end_hour: int | None = None
    end_minute: int | None = None


def validate_category(category: str) -> str:
    if category not in CATEGORIES:
        raise ValueError("Unknown form category.")
    return category


def sheet_tab_for(form_type: str, form_subtype: str | None = None) -> str:
    try:
        return FORM_TABS[(form_type, form_subtype)]
    except KeyError as exc:
        raise ValueError("Unknown form type.") from exc


def status_for_action(form_type: str, form_subtype: str | None, action: str) -> str:
    try:
        return ACTION_STATUSES[(form_type, form_subtype)][action]
    except KeyError as exc:
        raise ValueError("That review action is not valid for this form.") from exc


def allowed_actions(form_type: str, form_subtype: str | None) -> tuple[str, ...]:
    try:
        return tuple(ACTION_STATUSES[(form_type, form_subtype)])
    except KeyError as exc:
        raise ValueError("Unknown form type.") from exc


def local_datetime(day: date, hour: int, minute: int) -> tuple[datetime, datetime]:
    if minute not in {0, 15, 30, 45}:
        raise ValueError("Time must use a 15-minute increment.")
    naive = datetime.combine(day, time(hour=hour, minute=minute))
    local = naive.replace(tzinfo=EASTERN)
    round_trip = local.astimezone(timezone.utc).astimezone(EASTERN).replace(tzinfo=None)
    if round_trip != naive:
        raise ValueError("The selected New York time does not exist because of daylight saving time.")
    return local, local.astimezone(timezone.utc)


def form_datetimes(state: FormState) -> tuple[datetime, datetime, datetime | None, datetime | None]:
    if state.start_date is None or state.start_hour is None or state.start_minute is None:
        raise ValueError("A start date and time are required.")
    if state.start_hour not in START_HOURS or state.start_minute != 0:
        raise ValueError("Choose one of the available start times.")
    start_et, start_utc = local_datetime(state.start_date, state.start_hour, state.start_minute)
    if state.form_subtype == "timesheet_adjustment":
        if state.end_hour is None or state.end_minute is None:
            raise ValueError("An end time is required.")
        # ponytail: the end date is the selected start date; only its time varies.
        end_et, end_utc = local_datetime(state.start_date, state.end_hour, state.end_minute)
        if end_et <= start_et:
            raise ValueError("End time must be after start time.")
        return start_et, start_utc, end_et, end_utc
    return start_et, start_utc, None, None


def submission_prefix(form_type: str, form_subtype: str | None) -> str:
    prefixes = {
        ("when_i_work", "timesheet_adjustment"): "WIW-TA",
        ("when_i_work", "emergency_shift_release"): "WIW-ESR",
        ("question", None): "Q",
        ("concern", None): "C",
        ("feedback", None): "F",
    }
    try:
        return prefixes[(form_type, form_subtype)]
    except KeyError as exc:
        raise ValueError("Unknown form type.") from exc
