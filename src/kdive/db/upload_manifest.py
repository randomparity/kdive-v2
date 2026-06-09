"""Owner-scoped upload-manifest storage for external-build ingestion (ADR-0048 §4).

A manifest is the declared ``(name, sha256, size_bytes)`` set an agent commits at
the artifact upload tools for one owner (a CREATED Run or a DEFINED System), plus the
object-key ``prefix`` the reaper lists and the ``deadline`` it keys off. It is replaced
wholesale on a re-mint (one call, full set) and deleted when the owner finalizes or is
reaped. It is not the write-once ``artifacts`` row.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.components.uploads import ManifestEntry


class UploadManifest(NamedTuple):
    """A persisted manifest: the declared entries, the key prefix, and the deadline."""

    entries: tuple[ManifestEntry, ...]
    prefix: str
    deadline: datetime


@dataclass(frozen=True)
class UploadManifestReplaceRequest:
    """A full replacement for one owner's upload manifest."""

    owner_kind: str
    owner_id: UUID
    prefix: str
    entries: Sequence[ManifestEntry]
    ttl: timedelta


async def replace_manifest(
    conn: AsyncConnection,
    request: UploadManifestReplaceRequest,
) -> None:
    """Upsert the owner's manifest, stamping ``deadline = now() + ttl`` in Postgres.

    Full-set replace: a re-mint overwrites the prior manifest, prefix, and deadline.

    Args:
        conn: An async connection (autocommit or within a transaction).
        request: Owner, prefix, entries, and upload-window TTL for the replacement.
    """
    payload = [
        {"name": e.name, "sha256": e.sha256, "size_bytes": e.size_bytes} for e in request.entries
    ]
    await conn.execute(
        "INSERT INTO upload_manifests (owner_kind, owner_id, prefix, manifest, deadline) "
        "VALUES (%s, %s, %s, %s, now() + %s) "
        "ON CONFLICT (owner_kind, owner_id) DO UPDATE SET "
        "  prefix = EXCLUDED.prefix, manifest = EXCLUDED.manifest, deadline = EXCLUDED.deadline",
        (request.owner_kind, request.owner_id, request.prefix, Jsonb(payload), request.ttl),
    )


async def get_manifest(
    conn: AsyncConnection, owner_kind: str, owner_id: UUID
) -> UploadManifest | None:
    """Return the owner's manifest, or ``None`` if none is recorded.

    Args:
        conn: An async connection.
        owner_kind: The owning table name — ``'runs'`` or ``'systems'``.
        owner_id: The owning row's primary key.

    Returns:
        The persisted manifest, or ``None`` if no row exists for this owner.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT prefix, manifest, deadline FROM upload_manifests "
            "WHERE owner_kind = %s AND owner_id = %s",
            (owner_kind, owner_id),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    entries = tuple(
        ManifestEntry(e["name"], e["sha256"], int(e["size_bytes"])) for e in row["manifest"]
    )
    return UploadManifest(entries=entries, prefix=row["prefix"], deadline=row["deadline"])


async def delete_manifest(conn: AsyncConnection, owner_kind: str, owner_id: UUID) -> None:
    """Delete the owner's manifest row (idempotent — absent is fine).

    Args:
        conn: An async connection.
        owner_kind: The owning table name — ``'runs'`` or ``'systems'``.
        owner_id: The owning row's primary key.
    """
    await conn.execute(
        "DELETE FROM upload_manifests WHERE owner_kind = %s AND owner_id = %s",
        (owner_kind, owner_id),
    )
