"""Reachout rules that need no Discord or Google access: types, row validation, deadlines, IDs."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from integrations.google_sheets import field as roster_field, first_name, full_name, truthy

WEEKLY = "weekly_performance"
BEHAVIORS = "agent_behaviors"
OPT_OUTS = "opt_outs"
FLAGGED = "flagged_messages"
CONCERNING = "concerning"
REACHOUT_TYPES = {
    WEEKLY: "Weekly performance",
    BEHAVIORS: "Agent behaviors",
    OPT_OUTS: "Opt outs",
    FLAGGED: "Flagged messages",
    CONCERNING: "Concerning",
}
# ponytail: acknowledgment is a property of the category, not a Sheets column, so no
# row can remove it from these three.
ACK_REQUIRED_TYPES = frozenset({BEHAVIORS, OPT_OUTS, FLAGGED, CONCERNING})
EXAMPLE_PREFIX = "EXAMPLE-"
EXAMPLE_DOMAIN = "@example.invalid"


def is_example(value: str) -> bool:
    """Template identifiers that can never be sent to anyone."""
    value = value.strip().lower()
    return value.startswith(EXAMPLE_PREFIX.lower()) or value.endswith(EXAMPLE_DOMAIN)

MAX_TITLE = 150
MAX_BODY = 3500
MAX_URLS = 5
MAX_URL = 400  # with MAX_TITLE, MAX_BODY and five "[Link N](…)" links, the card stays under 6000
MAX_EXTERNAL_ID = 200
MAX_RESPONSE = 2000
MAX_REMINDER = 1000
DUE_FORMAT = "%Y-%m-%d %H:%M"
DISCORD_ID_RE = re.compile(r"^\d{17,20}$")

# Operator fields whose change after release means Discord no longer matches the row.
MATERIAL_FIELDS = (
    "agent_email", "reachout_type", "title", "message_body", "reference_urls", "due_at_override",
)

# Required items fall due, and the agent is reminded in the thread, this many calendar days after delivery.
DUE_AFTER_DAYS = 7

# Obligation types and states.
ACKNOWLEDGMENT = "ACKNOWLEDGMENT"
FOLLOW_UP = "FOLLOW_UP_RESPONSE"
OPEN_OBLIGATION_STATES = ("AWAITING_DELIVERY", "PENDING")

DEFAULT_ACK_NOTICE = (
    "Submitting this form records that you received and reviewed this reachout. "
    "It does not necessarily mean you agree with its contents."
)
DEFAULT_REMINDER_TEMPLATE = (
    "Reminder: your {requirement} for \"{title}\" was due {due_at}. "
    "Please use the button on the reachout message to submit it."
)
WEEKDAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    """Stored timestamps: UTC, second precision, so string order is time order."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def new_id(prefix: str, now: datetime | None = None) -> str:
    return f"{prefix}-{(now or utcnow()):%Y%m%d}-{secrets.token_hex(4).upper()}"


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone {name!r}; use an IANA name such as America/New_York.") from exc


@dataclass(frozen=True)
class ReachoutConfig:
    timezone: str = "America/New_York"
    business_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    deadline_business_days: int = 3
    holiday_calendars: tuple[str, ...] = ("default",)
    holidays: frozenset = frozenset()  # {(calendar_id, date)}
    nudge_repeat_business_days: int = 0
    reminder_template: str = DEFAULT_REMINDER_TEMPLATE
    acknowledgment_notice: str = DEFAULT_ACK_NOTICE

    @property
    def tz(self) -> ZoneInfo:
        return zone(self.timezone)

    def holiday_dates(self) -> frozenset[date]:
        return frozenset(day for calendar, day in self.holidays if calendar in self.holiday_calendars)

    def to_json(self) -> str:
        data = self.__dict__ | {
            "holidays": sorted([calendar, day.isoformat()] for calendar, day in self.holidays),
        }
        return json.dumps(data)

    @classmethod
    def from_json(cls, text: str) -> "ReachoutConfig":
        data = json.loads(text)
        data["business_weekdays"] = tuple(data["business_weekdays"])
        data["holiday_calendars"] = tuple(data["holiday_calendars"])
        data["holidays"] = frozenset((c, date.fromisoformat(d)) for c, d in data["holidays"])
        return cls(**data)


CONFIG_KEYS = {
    "timezone": "Operational IANA timezone for deadlines and Sheets timestamps.",
    "business_weekdays": "Comma-separated business days, e.g. MON,TUE,WED,THU,FRI.",
    "default_deadline_business_days": "Unused: required items are due 7 calendar days after delivery.",
    "nudge_repeat_business_days": "0 = one overdue reminder only; N = remind the agent again every N business days.",
    "reminder_template": "Standard agent reminder. Placeholders: {requirement} {title} {due_at}.",
    "acknowledgment_notice": "Text shown in every acknowledgment form.",
}


def parse_config(rows: list[dict], holiday_rows: list[dict]) -> tuple[ReachoutConfig, dict, dict]:
    """Return the config plus per-key and per-holiday-row status text for the Sheets view."""
    defaults = ReachoutConfig()
    values: dict = {}
    key_status: dict[str, str] = {}
    for row in rows:
        key = (row.get("key") or "").strip()
        raw = (row.get("value") or "").strip()
        if not key:
            continue
        if key not in CONFIG_KEYS:
            key_status[key] = "IGNORED: unknown key"
            continue
        if not raw:
            key_status[key] = "OK: blank, using default"
            continue
        try:
            values[key] = _config_value(key, raw)
            key_status[key] = "OK"
        except ValueError as exc:
            key_status[key] = f"INVALID: {exc} Using the default."
    holidays = set()
    holiday_status: dict[int, str] = {}
    for index, row in enumerate(holiday_rows):
        raw = (row.get("date") or "").strip()
        calendar = (row.get("calendar_id") or "").strip() or "default"
        if not raw:
            continue
        try:
            holidays.add((calendar, date.fromisoformat(raw)))
            holiday_status[index] = "OK"
        except ValueError:
            holiday_status[index] = "INVALID: use YYYY-MM-DD"
    config = ReachoutConfig(
        timezone=values.get("timezone", defaults.timezone),
        business_weekdays=values.get("business_weekdays", defaults.business_weekdays),
        deadline_business_days=values.get("default_deadline_business_days", defaults.deadline_business_days),
        holiday_calendars=values.get("holiday_calendars", defaults.holiday_calendars),
        holidays=frozenset(holidays),
        nudge_repeat_business_days=values.get("nudge_repeat_business_days", defaults.nudge_repeat_business_days),
        reminder_template=values.get("reminder_template", defaults.reminder_template),
        acknowledgment_notice=values.get("acknowledgment_notice", defaults.acknowledgment_notice),
    )
    return config, key_status, holiday_status


def _config_value(key: str, raw: str):
    if key == "timezone":
        zone(raw)
        return raw
    if key == "business_weekdays":
        names = [part.strip().upper()[:3] for part in raw.split(",") if part.strip()]
        if not names or any(name not in WEEKDAY_NAMES for name in names):
            raise ValueError("Use day names such as MON,TUE,WED,THU,FRI.")
        return tuple(sorted({WEEKDAY_NAMES.index(name) for name in names}))
    if key == "holiday_calendars":
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if key in {"default_deadline_business_days", "nudge_repeat_business_days"}:
        if not raw.isdigit():
            raise ValueError("Use a whole number.")
        number = int(raw)
        low = 1 if key == "default_deadline_business_days" else 0
        if not low <= number <= 30:
            raise ValueError(f"Use {low}–30.")
        return number
    if key == "reminder_template":
        try:
            raw.format(requirement="", title="", due_at="")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError("Only {requirement}, {title} and {due_at} placeholders are allowed.") from exc
        if len(raw) > 1500:
            raise ValueError("Keep it under 1500 characters.")
        return raw
    if len(raw) > 1500:
        raise ValueError("Keep it under 1500 characters.")
    return raw


# --- Business days -------------------------------------------------------------------------

def is_business_day(day: date, config: ReachoutConfig) -> bool:
    return day.weekday() in config.business_weekdays and day not in config.holiday_dates()


def localize(day: date, wall: time, tz: ZoneInfo) -> datetime:
    """A local wall-clock time as UTC.

    A wall time skipped by a DST change resolves to the same instant an hour later in wall
    time (02:30 → 03:30); a repeated wall time resolves to its first occurrence.
    """
    return datetime.combine(day, wall.replace(tzinfo=None, fold=0), tzinfo=tz).astimezone(timezone.utc)


def add_business_days(start: datetime, days: int, config: ReachoutConfig, tz: ZoneInfo | None = None) -> datetime:
    """The same local wall-clock time on the Nth business day after start's local date.

    Day one is the first business day after the start date, so a Friday 2 pm start with
    three days is Wednesday 2 pm, and a Saturday 10 am start is Wednesday 10 am.
    """
    tz = tz or config.tz
    local = start.astimezone(tz)
    day = local.date()
    counted = 0
    for _ in range(3660):
        if counted == days:
            return localize(day, local.time(), tz)
        day += timedelta(days=1)
        if is_business_day(day, config):
            counted += 1
    raise ValueError("No business days found within ten years; check business_weekdays and holidays.")


def business_deadline(delivered: datetime, config: ReachoutConfig, tz: ZoneInfo | None = None) -> datetime:
    return add_business_days(delivered, config.deadline_business_days, config, tz)


def parse_due_override(raw: str, config: ReachoutConfig) -> datetime:
    """`YYYY-MM-DD HH:MM` in the operational timezone, as UTC."""
    try:
        naive = datetime.strptime(raw.strip(), DUE_FORMAT)
    except ValueError as exc:
        raise ValueError(f"due_at_override must look like 2026-11-04 17:00 ({config.timezone}).") from exc
    aware = localize(naive.date(), naive.time(), config.tz)
    if aware.astimezone(config.tz).replace(tzinfo=None) != naive:
        raise ValueError("due_at_override falls in a daylight-saving gap; choose another time.")
    return aware


# --- Display -------------------------------------------------------------------------------

def local_text(value: datetime | str | None, tz: ZoneInfo) -> str:
    if isinstance(value, str):
        value = parse_iso(value)
    if value is None:
        return ""
    local = value.astimezone(tz)
    return f"{local:%a %b} {local.day}, {local.year} {local:%I:%M %p %Z}".replace(" 0", " ")


def sheet_time(value: str | None, tz: ZoneInfo) -> str:
    """Sheets timestamps: ISO 8601 in the operational timezone, with its UTC offset."""
    parsed = parse_iso(value)
    return parsed.astimezone(tz).isoformat() if parsed else ""


def discord_time(value: datetime | str | None) -> str:
    if isinstance(value, str):
        value = parse_iso(value)
    return f"<t:{int(value.timestamp())}:F>" if value else ""


# --- Queue rows ----------------------------------------------------------------------------

def input_hash(row: dict) -> str:
    material = [str(row.get(name) or "").strip() for name in MATERIAL_FIELDS]
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()[:16]


def wants_send(row: dict) -> bool:
    """A queue row is released by ticking its send checkbox."""
    return parse_bool(row.get("send")) is True


def parse_type(raw: str) -> str | None:
    text = raw.strip().lower()
    for key, label in REACHOUT_TYPES.items():
        if text in (key, label.lower()):
            return key
    return None


def parse_bool(raw: str | None, *, default: bool = False) -> bool | None:
    text = (raw or "").strip().upper()
    if not text:
        return default
    return {"TRUE": True, "FALSE": False}.get(text)


def split_urls(raw: str) -> list[str]:
    return [part for part in re.split(r"[\s,]+", raw.strip()) if part]


@dataclass
class Payload:
    """The snapshot that is delivered; later row edits never change it."""

    agent_id: str
    reachout_type: str
    title: str
    message_body: str
    reference_urls: list[str] = field(default_factory=list)
    response_text_required: bool = False
    due_at_override: str | None = None  # UTC ISO
    external_source_id: str | None = None
    performance_sentiment: str | None = None  # no longer set; kept so older snapshots still load

    @property
    def ack_required(self) -> bool:
        return self.reachout_type in ACK_REQUIRED_TYPES

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Payload":
        return cls(**json.loads(text))


def validate_row(row: dict, config: ReachoutConfig, now: datetime) -> tuple[Payload | None, list[str]]:
    """Validate a READY row's operator fields. Errors are written to Sheets as-is."""
    errors = []
    agent_id = (row.get("agent_email") or row.get("agent_id") or "").strip().lower()
    if not agent_id:
        errors.append("agent_email is required.")
    elif is_example(agent_id):
        errors.append("Example agents are template data and can never be sent.")
    reachout_type = parse_type(row.get("reachout_type") or "")
    if not reachout_type:
        errors.append("reachout_type must be one of: " + ", ".join(REACHOUT_TYPES.values()) + ".")
    title = (row.get("title") or "").strip()
    if not title:
        errors.append("title is required.")
    elif len(title) > MAX_TITLE:
        errors.append(f"title is {len(title)} characters; the limit is {MAX_TITLE}.")
    body = (row.get("message_body") or "").strip()
    if not body:
        errors.append("message_body is required.")
    elif len(body) > MAX_BODY:
        errors.append(f"message_body is {len(body)} characters; the limit is {MAX_BODY}.")
    urls = split_urls(row.get("reference_urls") or "")
    if len(urls) > MAX_URLS:
        errors.append(f"reference_urls has {len(urls)} links; the limit is {MAX_URLS}.")
    for url in urls:
        if not re.match(r"^https?://\S+$", url) or len(url) > MAX_URL:
            errors.append(f"reference_urls entry {url[:60]!r} must be an http(s) link under {MAX_URL} characters.")
    # Every category except Weekly performance requires a written response; the
    # response_text_required column is no longer read.
    text_required = reachout_type != WEEKLY
    due_override = None
    raw_due = (row.get("due_at_override") or "").strip()
    if raw_due:
        if reachout_type == WEEKLY:
            errors.append("due_at_override only applies to categories that require acknowledgment.")
        else:
            try:
                parsed = parse_due_override(raw_due, config)
                if parsed <= now:
                    errors.append("due_at_override is in the past.")
                due_override = iso(parsed)
            except ValueError as exc:
                errors.append(str(exc))
    external = (row.get("external_source_id") or "").strip() or None
    if external and len(external) > MAX_EXTERNAL_ID:
        errors.append(f"external_source_id is limited to {MAX_EXTERNAL_ID} characters.")
    if errors:
        return None, errors
    return Payload(
        agent_id=agent_id,
        reachout_type=reachout_type,
        title=title,
        message_body=body,
        reference_urls=urls,
        response_text_required=text_required,
        due_at_override=due_override,
        external_source_id=external,
    ), []


# --- Agent directory -----------------------------------------------------------------------

LEAD_EMAIL_COLUMN = "Team Lead Email"


def directory_key(row: dict) -> str:
    """An agent's ID: agent_id if given, otherwise their work email."""
    return (row.get("agent_id") or "").strip() or (row.get("agent_email") or "").strip().lower()


def roster_lead_email(roster_record: dict | None) -> str:
    return roster_field(roster_record, LEAD_EMAIL_COLUMN).lower()


def evaluate_agent_row(row: dict, roster_record: dict | None, verified_discord_id: int | None,
                       lead_discord_id: int | None) -> dict:
    """An agent directory row as stored values plus directory_status/directory_error.

    The lead is the roster's Team Lead Email, resolved to a Discord ID through verification.
    """
    email = (row.get("agent_email") or "").strip().lower()
    agent_id = directory_key(row)
    lead_email = roster_lead_email(roster_record)
    values = {
        "agent_id": agent_id,
        "agent_email": email,
        "agent_name": full_name(roster_record),
        "agent_first_name": first_name(roster_record) or full_name(roster_record).split(" ")[0],
        "agent_discord_user_id": verified_discord_id,
        "lead_email": lead_email,
        "lead_discord_user_id": lead_discord_id,
        "active": 0,
        "team": (row.get("team") or "").strip(),
        "timezone": (row.get("timezone_override") or "").strip(),
        "fallback_channel_id": None,
        "existing_channel_id": None,
        "directory_status": "OK",
        "directory_error": "",
    }
    errors = []
    if is_example(agent_id):
        return values | {"directory_status": "EXAMPLE", "directory_error": "Template row; never used."}
    active = parse_bool(row.get("active"))
    if active is None:
        errors.append("active must be TRUE or FALSE.")
    if not email:
        errors.append("agent_email is required.")
    elif roster_record is None:
        errors.append("agent_email is not in the Discord Access roster.")
    for name in ("fallback_channel_id", "existing_channel_id"):
        raw = (row.get(name) or "").strip()
        if raw and not DISCORD_ID_RE.match(raw):
            errors.append(f"{name} must be a 17–20 digit Discord ID formatted as plain text.")
        elif raw:
            values[name] = int(raw)
    if values["timezone"]:
        try:
            zone(values["timezone"])
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        return values | {"directory_status": "INVALID", "directory_error": " ".join(errors)}
    values["active"] = int(bool(active) and truthy(roster_record.get("Active")))
    if not values["agent_name"]:
        return values | {"directory_status": "INVALID", "directory_error": "The Discord Access roster has no First Name / Last Name for this email."}
    if not lead_email:
        return values | {"directory_status": "NO_LEAD", "directory_error":
                         f"No {LEAD_EMAIL_COLUMN} in Discord Access (e.g. Leadership); reachouts are not sent."}
    if verified_discord_id is None:
        return values | {"directory_status": "BLOCKED", "directory_error": "The agent has not completed Discord work-email verification."}
    if lead_discord_id is None:
        return values | {"directory_status": "BLOCKED", "directory_error":
                         f"The team lead ({lead_email}) has not completed Discord work-email verification."}
    if verified_discord_id == values["lead_discord_user_id"]:
        return values | {"directory_status": "INVALID", "directory_error": "An agent cannot be their own lead."}
    if not values["active"]:
        reason = "active is FALSE." if not active else "The Discord Access roster marks this email inactive."
        return values | {"directory_status": "INACTIVE", "directory_error": reason}
    return values
