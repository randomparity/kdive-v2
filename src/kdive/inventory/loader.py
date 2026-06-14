"""Read + parse + validate ``systems.toml`` into a typed model (ADR-0112).

Every failure path — file missing/unreadable, malformed TOML, non-UTF-8 bytes, or
schema validation failure — is converted to :class:`InventoryError`, so a caller
fault-isolates one exception type.

:func:`load_inventory` treats an absent file as an error (the operator named a path
that is not there). :func:`load_inventory_optional` treats an absent file as "nothing
declared" (``None``) — use it on the default path, since ``systems.toml`` is gitignored
and a fresh deploy legitimately has no file yet.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import ValidationError

from kdive.inventory.errors import InventoryError
from kdive.inventory.model import InventoryDoc


def load_inventory(path: Path) -> InventoryDoc:
    """Read, parse, and validate ``systems.toml`` into an :class:`InventoryDoc`.

    Args:
        path: The ``systems.toml`` path to load.

    Returns:
        The validated inventory document.

    Raises:
        InventoryError: File missing/unreadable, malformed TOML, non-UTF-8 bytes,
            or schema validation failure — always this type.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InventoryError(str(path), "file", f"cannot read: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise InventoryError(str(path), "toml", f"malformed: {exc}") from exc
    try:
        return InventoryDoc.parse(data)
    except ValidationError as exc:  # pragma: no cover - parse converts these
        raise InventoryError(str(path), "schema", str(exc)) from exc


def load_inventory_optional(path: Path) -> InventoryDoc | None:
    """Like :func:`load_inventory`, but a missing file returns ``None``.

    Use this on the default path: ``systems.toml`` is gitignored, so an absent default
    is the normal pre-config state, not an operator error. A present-but-malformed file
    still raises :class:`InventoryError`.

    Args:
        path: The default ``systems.toml`` path to load if present.

    Returns:
        The validated document, or ``None`` if the file is absent.

    Raises:
        InventoryError: The file is present but unreadable/malformed/invalid.
    """
    if not path.exists():
        return None
    return load_inventory(path)
