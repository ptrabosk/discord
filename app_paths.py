"""Application paths that do not depend on the process working directory."""

from pathlib import Path
from typing import Optional


APP_ROOT = Path(__file__).resolve().parent
ENV_FILE = APP_ROOT / ".env"


def resolve_app_path(value: Optional[str], default: str) -> Path:
    """Resolve configuration paths relative to the checked-out application."""
    path = Path(value.strip()) if value and value.strip() else Path(default)
    if not path.is_absolute():
        path = APP_ROOT / path
    return path.resolve()
