"""Provider-runtime naming and host artifact path helpers."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

_CONSOLE_DIR = "/var/lib/kdive/console"


def domain_name_for(system_id: UUID) -> str:
    return f"kdive-{system_id}"


def console_log_path(system_id: UUID) -> Path:
    return Path(_CONSOLE_DIR) / f"{system_id}.log"


def read_console_log(path: Path) -> bytes:
    """Read a System console log; absent or unreadable logs are treated as empty."""
    try:
        return path.read_bytes()
    except (FileNotFoundError, PermissionError):
        return b""
