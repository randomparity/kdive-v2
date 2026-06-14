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
from typing import Any, Literal, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.provider_components.uploads import ChunkEntry, ManifestEntry

UploadOwnerKind = Literal["runs", "systems"]
RUN_UPLOAD_OWNER: UploadOwnerKind = "runs"
SYSTEM_UPLOAD_OWNER: UploadOwnerKind = "systems"


class UploadManifest(NamedTuple):
    """A persisted manifest: the declared entries, the key prefix, and the deadline."""

    entries: tuple[ManifestEntry, ...]
    prefix: str
    deadline: datetime


@dataclass(frozen=True)
class UploadManifestReplaceRequest:
    """A full replacement for one owner's upload manifest."""

    owner_kind: UploadOwnerKind
    owner_id: UUID
    prefix: str
    entries: Sequence[ManifestEntry]
    ttl: timedelta


def _entry_payload(entry: ManifestEntry) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": entry.name,
        "sha256": entry.sha256,
        "size_bytes": entry.size_bytes,
    }
    if entry.chunks is not None:
        payload["chunks"] = [{"sha256": c.sha256, "size_bytes": c.size_bytes} for c in entry.chunks]
    return payload


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
    payload = [_entry_payload(e) for e in request.entries]
    await conn.execute(
        "INSERT INTO upload_manifests (owner_kind, owner_id, prefix, manifest, deadline) "
        "VALUES (%s, %s, %s, %s, now() + %s) "
        "ON CONFLICT (owner_kind, owner_id) DO UPDATE SET "
        "  prefix = EXCLUDED.prefix, manifest = EXCLUDED.manifest, deadline = EXCLUDED.deadline",
        (request.owner_kind, request.owner_id, request.prefix, Jsonb(payload), request.ttl),
    )


async def refresh_deadline(
    conn: AsyncConnection, owner_kind: UploadOwnerKind, owner_id: UUID, ttl: timedelta
) -> bool:
    """Set ``deadline = now() + ttl`` if a non-expired manifest exists; report whether it did.

    Returns ``False`` when no row exists OR the current deadline is already past — the caller
    treats the latter as an expired upload window (ADR-0104 §6 step A). Refreshing the deadline
    under the per-Run lock the reaper also takes is what stops the reaper from reclaiming an
    in-flight reassembly's chunk objects.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE upload_manifests SET deadline = now() + %s "
            "WHERE owner_kind = %s AND owner_id = %s AND deadline >= now()",
            (ttl, owner_kind, owner_id),
        )
        return cur.rowcount == 1


async def get_manifest(
    conn: AsyncConnection, owner_kind: UploadOwnerKind, owner_id: UUID
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
    entries = tuple(_entry_from_payload(e) for e in row["manifest"])
    return UploadManifest(entries=entries, prefix=row["prefix"], deadline=row["deadline"])


def _entry_from_payload(payload: Any) -> ManifestEntry:
    raw_chunks = payload.get("chunks")
    chunks = (
        tuple(ChunkEntry(c["sha256"], int(c["size_bytes"])) for c in raw_chunks)
        if isinstance(raw_chunks, list)
        else None
    )
    return ManifestEntry(
        payload["name"], payload["sha256"], int(payload["size_bytes"]), chunks=chunks
    )


async def delete_manifest(
    conn: AsyncConnection, owner_kind: UploadOwnerKind, owner_id: UUID
) -> None:
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
