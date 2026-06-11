"""Row-first publish/register two-write for catalog images (ADR-0092, issue #285).

``publish_image`` registers the catalog row **before** the object, so a rowless object can
never exist during a live publish (the window in which ``leaked_images`` could race the write).
It adopts the identity's existing ``defined``/``pending`` row (or inserts a fresh ``pending``
row), sets its ``object_key``, writes the qcow2 to the image prefix, gates on ``store.head()``,
then flips the row to ``registered`` and returns it.

Publish is **idempotent on the identity ``(provider, name, arch)``**: a re-run after a crashed
attempt adopts the in-flight ``pending`` row and re-arms its ``pending_since`` rather than
colliding. The recovery path for a crash mid-publish is the reconciler, not a bespoke rollback —
the leftover ``pending`` row and (possibly absent) object are swept by the deadline-guarded
``leaked_images``/``dangling_images`` sweeps once past the publish grace.

The blocking object-store calls (boto3) are offloaded via ``asyncio.to_thread`` so the worker
event loop never stalls behind a multi-GiB upload.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.cursor_async import AsyncCursor
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import (
    ImageCatalogEntry,
    ImageState,
    ImageVisibility,
    Sensitivity,
)
from kdive.provider_components import artifacts as artifact_types

_RETENTION_CLASS = "image"


class ImageObjectStore(Protocol):
    """The narrow object-store capability publish needs (an :class:`ObjectStore` satisfies it)."""

    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact: ...

    def head(self, key: str) -> artifact_types.HeadResult | None: ...


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """The fields needed to create an image row — not a built :class:`ImageCatalogEntry`.

    ``publish_image`` assigns the row's ``id``/``object_key``/``state``/``pending_since``; this
    request carries only the caller-supplied identity, boot layout, content digest, and scope.

    Attributes:
        provider: The provider whose plane built the image (e.g. ``"local-libvirt"``).
        name: The catalog image name.
        arch: The target architecture.
        format: The image format (e.g. ``"qcow2"``).
        root_device: The guest root device path (e.g. ``"/dev/vda"``).
        digest: The qcow2 content digest (``"sha256:<hex>"``) — the image identity, which the
            materialization fetch verifies the downloaded bytes against.
        capabilities: The guest-contract tags the image satisfies.
        provenance: The pinned build inputs/args, JSONB-serializable for the row.
        visibility: ``"public"`` or ``"private"``.
        owner: The owning project — set iff ``visibility`` is ``"private"``.
        expires_at: The private-image TTL deadline — set iff ``visibility`` is ``"private"``.
    """

    provider: str
    name: str
    arch: str
    format: str
    root_device: str
    digest: str
    capabilities: tuple[str, ...]
    provenance: dict[str, object]
    visibility: str
    owner: str | None = None
    expires_at: datetime | None = None


def image_object_key(provider: str, name: str, arch: str) -> str:
    """The object-store key for a catalog image, identity-keyed under the ``images/`` prefix.

    Matches the key the materialization fetch reads (``images/{provider}/{name}/{arch}.qcow2``);
    one registered row per identity means the identity-keyed object is unambiguous.
    """
    return f"images/{provider}/{name}/{arch}.qcow2"


async def _adopt_or_insert_pending(
    conn: AsyncConnection, request: PublishRequest, object_key: str
) -> UUID:
    """Adopt the identity's existing non-registered row, or insert a fresh ``pending`` row.

    Runs in one transaction so concurrent re-runs of the same identity serialize on the adopted
    row. A ``defined`` baseline and a crashed ``pending`` attempt are both adopted in place and
    moved to ``pending`` with ``object_key`` set and ``pending_since`` re-armed; resolution never
    returns either, so an adopted row is never visible mid-publish.
    """
    select_q = sql.SQL(
        "SELECT id FROM image_catalog "
        "WHERE provider = %(provider)s AND name = %(name)s AND arch = %(arch)s "
        "AND state IN (%(defined)s, %(pending)s) "
        "ORDER BY CASE WHEN state = %(pending)s THEN 0 ELSE 1 END "
        "FOR UPDATE LIMIT 1"
    )
    params = {
        "provider": request.provider,
        "name": request.name,
        "arch": request.arch,
        "defined": ImageState.DEFINED.value,
        "pending": ImageState.PENDING.value,
    }
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(select_q, params)
        existing = await cur.fetchone()
        if existing is not None:
            await cur.execute(
                "UPDATE image_catalog "
                "SET state = %s, object_key = %s, pending_since = now() WHERE id = %s",
                (ImageState.PENDING.value, object_key, existing["id"]),
            )
            return existing["id"]
        return await _insert_pending(cur, request, object_key)


async def _insert_pending(
    cur: AsyncCursor[DictRow], request: PublishRequest, object_key: str
) -> UUID:
    """Insert a fresh ``pending`` row from ``request`` and return its id.

    ``cur`` is a ``dict_row`` cursor already inside the adopt transaction.
    """
    insert_q = (
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, digest, capabilities, "
        " provenance, visibility, owner, expires_at, state, pending_since) "
        "VALUES (%(provider)s, %(name)s, %(arch)s, %(format)s, %(root_device)s, %(object_key)s, "
        " %(digest)s, %(capabilities)s, %(provenance)s, %(visibility)s, %(owner)s, "
        " %(expires_at)s, %(state)s, now()) RETURNING id"
    )
    params = {
        "provider": request.provider,
        "name": request.name,
        "arch": request.arch,
        "format": request.format,
        "root_device": request.root_device,
        "object_key": object_key,
        "digest": request.digest,
        "capabilities": list(request.capabilities),
        "provenance": Jsonb(request.provenance),
        "visibility": request.visibility,
        "owner": request.owner,
        "expires_at": request.expires_at,
        "state": ImageState.PENDING.value,
    }
    await cur.execute(insert_q, params)
    row = await cur.fetchone()
    if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
        raise RuntimeError("INSERT into image_catalog returned no row")
    return row["id"]


async def _write_object(store: ImageObjectStore, request: PublishRequest, data: bytes) -> None:
    """Write the qcow2 object for ``request``'s identity (offloaded; boto3 is synchronous)."""
    write_request = artifact_types.ArtifactWriteRequest(
        tenant="images",
        owner_kind=request.provider,
        owner_id=request.name,
        name=f"{request.arch}.qcow2",
        data=data,
        sensitivity=Sensitivity.REDACTED,
        retention_class=_RETENTION_CLASS,
    )
    await asyncio.to_thread(store.put_artifact, write_request)


async def _registered(conn: AsyncConnection, row_id: UUID) -> ImageCatalogEntry:
    """Flip ``row_id`` to ``registered`` and return the persisted row."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE image_catalog SET state = %s WHERE id = %s RETURNING *",
            (ImageState.REGISTERED.value, row_id),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: the row was just written as pending.
        raise RuntimeError(f"image_catalog row {row_id} vanished before registration")
    return ImageCatalogEntry.model_validate(row)


async def publish_image(
    conn: AsyncConnection, store: ImageObjectStore, *, request: PublishRequest, source: Path
) -> ImageCatalogEntry:
    """Row-first two-write publish: pending row → object → HEAD-gate → ``registered``.

    Adopts the identity's existing ``defined``/``pending`` row (or inserts a ``pending`` row from
    ``request``), sets its ``object_key``, writes the object at ``source`` to the image prefix,
    HEAD-gates, then flips the row to ``registered`` and returns it. Idempotent on
    ``(provider, name, arch)``: a re-run adopts the in-flight ``pending`` row and re-arms its
    ``pending_since``. Realizing a seeded ``defined`` baseline is this same path.

    Args:
        conn: An async Postgres connection (autocommit; the adopt step opens its own
            transaction).
        store: The image object store.
        request: The image identity, layout, digest, and scope.
        source: The local path to the built qcow2 to publish.

    Returns:
        The persisted ``registered`` :class:`ImageCatalogEntry`.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` if the object write or HEAD gate fails (the
            row stays ``pending`` for the reconciler to recover).
    """
    _ = ImageVisibility(request.visibility)  # fail-fast on an invalid visibility string
    object_key = image_object_key(request.provider, request.name, request.arch)
    row_id = await _adopt_or_insert_pending(conn, request, object_key)

    data = await asyncio.to_thread(source.read_bytes)
    await _write_object(store, request, data)

    head = await asyncio.to_thread(store.head, object_key)
    if head is None:
        raise CategorizedError(
            "published image object is not present after write (HEAD gate failed)",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"object_key": object_key},
        )
    return await _registered(conn, row_id)
