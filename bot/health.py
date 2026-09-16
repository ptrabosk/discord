import asyncio
import os
import subprocess
from datetime import datetime, timezone
from typing import Optional

from app_paths import APP_ROOT


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(value: Optional[datetime]) -> str:
    if value is None:
        return "Never"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_uptime(started_at: datetime) -> str:
    seconds = max(0, int((utc_now() - started_at).total_seconds()))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def get_bot_version() -> str:
    """Return an explicitly configured version or the checked-out Git commit."""
    configured = os.getenv("BOT_VERSION", "").strip()
    if configured:
        return configured
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={APP_ROOT}",
                "rev-parse",
                "--short",
                "HEAD",
            ],
            cwd=APP_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "Unavailable"
    return result.stdout.strip() or "Unavailable"


def smtp_configuration_status() -> tuple[bool, str]:
    required = ("SMTP_HOST", "SMTP_FROM")
    if any(not os.getenv(name, "").strip() for name in required):
        return False, "Missing required settings"
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError:
        return False, "Invalid port"
    if not 1 <= port <= 65_535:
        return False, "Invalid port"
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    if bool(username) != bool(password):
        return False, "Incomplete authentication settings"
    return True, "Configured (not connection-tested)"


async def check_google_sheet(sheet, timeout_seconds: float = 10) -> tuple[bool, str]:
    """Perform a read-only Google worksheet access check with a time limit."""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(sheet.check_access),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        return False, type(exc).__name__
    return True, "OK"
