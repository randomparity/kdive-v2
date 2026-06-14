"""Provider-runtime naming and host artifact path helpers."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory

_CONSOLE_DIR = "/var/lib/kdive/console"

# The deterministic System domain name carries the owning System's UUID (ADR-0111). Anchored
# so the ephemeral build-VM form (kdive-build-<uuid>) cannot match: the "build-" infix is not
# hex, so it never satisfies the leading hex group. Hex is matched case-insensitively.
_SYSTEM_DOMAIN_RE = re.compile(
    r"^kdive-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def domain_name_for(system_id: UUID) -> str:
    return f"kdive-{system_id}"


def system_id_from_domain_name(name: str) -> UUID | None:
    """The owning System UUID encoded in a ``kdive-<uuid>`` domain name, or ``None``.

    The inverse of :func:`domain_name_for`. Returns ``None`` for any name that is not a bare
    System domain — foreign names, the build-VM form ``kdive-build-<uuid>``, other prefixed
    forms, and anything not UUID-shaped — so a non-matching name is treated as unmanaged and
    never reaped by the reconciler's name-fallback path.
    """
    match = _SYSTEM_DOMAIN_RE.match(name)
    if match is None:
        return None
    try:
        return UUID(match.group(1))
    except ValueError:  # pragma: no cover - the regex already constrains the shape
        return None


def console_log_path(system_id: UUID) -> Path:
    return Path(_CONSOLE_DIR) / f"{system_id}.log"


def read_console_log(path: Path) -> bytes:
    """Read a System console log; absent logs are treated as empty."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as err:
        raise CategorizedError(
            "failed to read console log",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={
                "operation": "read_console_log",
                "path": str(path),
                "error": type(err).__name__,
            },
        ) from err
