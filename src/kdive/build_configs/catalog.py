"""Build-config catalog repository (ADR-0096): name -> sha256-verified fragment bytes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection, Connection
from psycopg.rows import dict_row

from kdive.domain.errors import CategorizedError, ErrorCategory

_SELECT = (
    "SELECT name, object_key, sha256, description FROM build_config_catalog WHERE name = %(name)s"
)


@dataclass(frozen=True)
class BuildConfigEntry:
    """One build_config_catalog row."""

    name: str
    object_key: str
    sha256: str
    description: str

    def verify_bytes(self, data: bytes) -> None:
        """Raise INFRASTRUCTURE_FAILURE if ``data`` does not hash to this row's ``sha256``.

        Args:
            data: The raw bytes to verify against the stored digest.

        Raises:
            CategorizedError: INFRASTRUCTURE_FAILURE when the sha256 does not match.
        """
        actual = hashlib.sha256(data).hexdigest()
        if actual != self.sha256:
            raise CategorizedError(
                "build-config object bytes do not match the catalog sha256",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"name": self.name},
            )


def parse_build_config_row(row: dict[str, Any]) -> BuildConfigEntry:
    """Map a DB row mapping to a :class:`BuildConfigEntry`.

    Args:
        row: A dict-shaped DB row with keys name, object_key, sha256, description.

    Returns:
        The parsed catalog entry.
    """
    return BuildConfigEntry(
        name=row["name"],
        object_key=row["object_key"],
        sha256=row["sha256"],
        description=row["description"],
    )


async def get_build_config(conn: AsyncConnection, name: str) -> BuildConfigEntry | None:
    """Return the catalog entry for ``name``, or ``None`` if absent (async, for the MCP tool).

    Args:
        conn: An open async psycopg connection.
        name: The fragment name to look up (e.g. ``"kdump"``).

    Returns:
        The matching :class:`BuildConfigEntry`, or ``None`` if no row exists.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SELECT, {"name": name})
        row = await cur.fetchone()
    return parse_build_config_row(row) if row is not None else None


def get_build_config_sync(conn: Connection, name: str) -> BuildConfigEntry | None:
    """Return the catalog entry for ``name``, or ``None`` (sync, for the build path).

    The provider build runs off the event loop via ``asyncio.to_thread`` and cannot await, so
    its catalog fetch uses a synchronous connection. Same query as the async variant.

    Args:
        conn: An open sync psycopg connection.
        name: The fragment name to look up (e.g. ``"kdump"``).

    Returns:
        The matching :class:`BuildConfigEntry`, or ``None`` if no row exists.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT, {"name": name})
        row = cur.fetchone()
    return parse_build_config_row(row) if row is not None else None
