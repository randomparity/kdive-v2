"""Image-catalog drift repair for the reconciler (M2.4/6, ADR-0092, ADR-0093).

Three deadline-guarded sweeps, modeled on :func:`kdive.reconciler.uploads.repair_abandoned_uploads`
(a ``deadline < now()`` window + a table cross-check, never an eager delete), each isolated on a
fresh pooled connection and each evaluating time in Postgres ``now()`` (never a Python clock):

* :func:`repair_leaked_images` — an object under the image prefix with **no catalog row** at all,
  older than the publish grace (keyed off the object's store mtime): delete the object. A
  ``pending`` row owns its object (the row is written before the object in the row-first publish),
  so a live publish is never raced; the mtime grace is the second fence against a just-written
  object whose row commit is in flight.
* :func:`repair_dangling_images` — a non-``defined`` row (``object_key IS NOT NULL``) whose object
  HEAD is missing **past its publish deadline** (``pending_since + grace``): remove the row. An
  object-less ``defined`` baseline is object-less by design and never dangling — it is skipped.
* :func:`repair_expired_private_images` — a ``private`` row with ``expires_at < now()``: delete
  the object and the row, **reference-guarded** (an image a non-terminal System still references
  through its ``provisioning_profile`` catalog rootfs is skipped, deferring its expiry) and
  **extend-fenced** (the ``expires_at`` is re-read under a per-row lock, the ADR-0036 renew
  analogue, so a concurrent operator extend committed after candidate selection is honored).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.domain.models import ImageState, ImageVisibility, ResourceKind
from kdive.domain.state import SystemState
from kdive.provider_components.artifacts import ObjectListing

_log = logging.getLogger(__name__)

_DEFINED_STATE = ImageState.DEFINED.value
_PRIVATE_VISIBILITY = ImageVisibility.PRIVATE.value
_TERMINAL_SYSTEM_STATES = (SystemState.TORN_DOWN, SystemState.FAILED)
_TERMINAL_SYSTEM_STATE_VALUES = tuple(state.value for state in _TERMINAL_SYSTEM_STATES)
# A catalog rootfs only ever appears under the local-libvirt provider section (the remote
# provider boots an operator-staged base image, the mock provider owns no rootfs), so the
# reference-guard's JSONB-containment probe keys that section's catalog ref.
_LOCAL_LIBVIRT_SECTION = ResourceKind.LOCAL_LIBVIRT.value


# The image sweeps consume the shared object-listing value type (key + store mtime); the
# alias keeps the reconciler tests' import surface stable while reusing one definition.
ImageMtime = ObjectListing


@runtime_checkable
class ImageSweepStore(Protocol):
    """The narrow object-store port the image sweeps consume."""

    def list_image_objects(self) -> list[ObjectListing]: ...
    def head_present(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


async def repair_leaked_images(
    conn: AsyncConnection, store: ImageSweepStore, grace: timedelta
) -> int:
    """Delete image-prefix objects with no catalog row, older than the publish grace.

    A live publish writes the catalog row **before** the object (ADR-0092 §3), so a rowless
    object is an orphan — a manual upload, or a build that wrote bytes before any row. The
    object's store mtime is compared against ``now() - grace`` **in Postgres**, so a freshly
    written object (a publish whose row commit is still in flight) is protected by the grace
    window and never raced. Each candidate's row-absence is re-checked immediately before the
    delete so a row that landed between the listing and the delete protects its object.

    Returns the number of objects deleted; one structured-log line per delete.
    """
    objects = await asyncio.to_thread(store.list_image_objects)
    deleted = 0
    for obj in objects:
        if await _delete_if_leaked(conn, store, obj, grace):
            deleted += 1
    return deleted


async def _delete_if_leaked(
    conn: AsyncConnection, store: ImageSweepStore, obj: ImageMtime, grace: timedelta
) -> bool:
    """Delete ``obj`` iff no catalog row references it and it is older than the grace window."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT EXISTS (SELECT 1 FROM image_catalog WHERE object_key = %s) OR %s >= now() - %s",
            (obj.key, obj.last_modified, grace),
        )
        row = await cur.fetchone()
    protected = bool(row[0]) if row is not None else True
    if protected:
        return False
    await asyncio.to_thread(store.delete, obj.key)
    _log.info("reconciler: leaked image object %s deleted (no row, past grace)", obj.key)
    return True


async def repair_dangling_images(
    conn: AsyncConnection, store: ImageSweepStore, grace: timedelta
) -> int:
    """Remove non-``defined`` rows whose object HEAD is missing past the publish deadline.

    Candidates are rows with ``object_key IS NOT NULL`` (so an object-less ``defined`` baseline
    is excluded — it is object-less by design, never dangling) whose ``pending_since + grace``
    deadline has elapsed (``pending_since < now() - grace``, evaluated in Postgres). For each, the
    object's presence is HEAD-checked; a row whose object is present is left alone, a row whose
    object is missing is deleted under a re-read fenced on the same deadline (so a re-armed
    ``pending_since`` from a concurrent re-publish defers removal).

    Returns the number of rows removed; one structured-log line per removal.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, object_key FROM image_catalog "
            "WHERE object_key IS NOT NULL AND state <> %s AND pending_since < now() - %s",
            (_DEFINED_STATE, grace),
        )
        candidates = await cur.fetchall()
    removed = 0
    for cand in candidates:
        if await asyncio.to_thread(store.head_present, cand["object_key"]):
            continue
        if await _remove_dangling_row(conn, cand["id"], grace):
            removed += 1
    return removed


async def _remove_dangling_row(conn: AsyncConnection, row_id: UUID, grace: timedelta) -> bool:
    """Delete one dangling row, fenced on its deadline by the delete predicate.

    The ``DELETE ... WHERE pending_since < now() - grace`` re-validates the deadline against
    Postgres ``now()`` atomically, so a row whose ``pending_since`` was re-armed (a concurrent
    re-publish) since candidate selection is declined and a publish in flight is never wedged.
    """
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM image_catalog "
            "WHERE id = %s AND object_key IS NOT NULL AND state <> %s "
            "  AND pending_since < now() - %s",
            (row_id, _DEFINED_STATE, grace),
        )
        removed = cur.rowcount
    if removed:
        _log.info(
            "reconciler: dangling image row %s removed (object missing past deadline)", row_id
        )
    return bool(removed)


async def repair_expired_private_images(conn: AsyncConnection, store: ImageSweepStore) -> int:
    """Delete object + row for expired private images, reference-guarded and extend-fenced.

    Candidates are ``private`` rows with ``expires_at < now()`` (Postgres clock). Each is pruned
    only if it is **not referenced** by a non-terminal System (a JSONB-containment check against
    ``systems.provisioning_profile`` — a referenced image's expiry defers) and the per-row locked
    re-read still observes it expired (the extend fence honors a concurrent operator extend). The
    object is deleted before the row so a crash leaves a dangling row the dangling sweep heals,
    never a rowless object the leaked sweep would re-discover under its grace.

    Returns the number of images pruned; one structured-log line per prune.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, object_key FROM image_catalog "
            "WHERE visibility = %s AND expires_at IS NOT NULL AND expires_at < now()",
            (_PRIVATE_VISIBILITY,),
        )
        candidates = await cur.fetchall()
    pruned = 0
    for cand in candidates:
        if await _image_referenced(conn, cand["id"]):
            _log.info(
                "reconciler: expired private image %s referenced by a non-terminal System; "
                "deferring expiry",
                cand["id"],
            )
            continue
        if await expire_one_private_image(conn, store, cand["id"], cand["object_key"]):
            pruned += 1
    return pruned


async def _image_referenced(conn: AsyncConnection, row_id: UUID) -> bool:
    """True if a non-terminal System references this image's catalog rootfs.

    The image's ``(provider, name)`` identity is folded into a JSONB-containment (``@>``) probe
    of each non-terminal System's ``provisioning_profile``: a catalog rootfs is serialized under
    the local-libvirt provider section as ``{"kind": "catalog", "provider": ..., "name": ...}``.
    Containment is an FK-free reference check — an image is never hard-linked to a System, so a
    referenced image's expiry simply defers rather than failing a delete.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT provider, name FROM image_catalog WHERE id = %s", (row_id,))
        image = await cur.fetchone()
    if image is None:
        return False
    probe = Jsonb(
        {
            "provider": {
                _LOCAL_LIBVIRT_SECTION: {
                    "rootfs": {
                        "kind": "catalog",
                        "provider": image["provider"],
                        "name": image["name"],
                    }
                }
            }
        }
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM systems WHERE state <> ALL(%s) AND provisioning_profile @> %s LIMIT 1",
            (list(_TERMINAL_SYSTEM_STATE_VALUES), probe),
        )
        return await cur.fetchone() is not None


async def expire_one_private_image(
    conn: AsyncConnection, store: ImageSweepStore, row_id: UUID, object_key: str | None
) -> bool:
    """Delete one expired private image's object + row under a ``FOR UPDATE`` row lock.

    The ``SELECT ... FOR UPDATE`` re-read is the extend fence (the ADR-0036 renew analogue): it
    locks the row and declines one whose ``expires_at`` was pushed into the future since candidate
    selection (a concurrent operator extend) or that another pass already pruned. Re-validated
    against Postgres ``now()``. The row stays locked across the object delete and the row delete,
    so an extend committing after the fence read blocks until this prune commits or rolls back.
    The object delete (idempotent) precedes the row delete so a crash strands at most a dangling
    row the dangling sweep heals, never a rowless object.

    Returns ``True`` if this call pruned the image, ``False`` if the fence declined it.
    """
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM image_catalog "
            "WHERE id = %s AND visibility = %s "
            "  AND expires_at IS NOT NULL AND expires_at < now() FOR UPDATE",
            (row_id, _PRIVATE_VISIBILITY),
        )
        still_expired = await cur.fetchone() is not None
        if not still_expired:
            return False
        if object_key is not None:
            await asyncio.to_thread(store.delete, object_key)
        await cur.execute("DELETE FROM image_catalog WHERE id = %s", (row_id,))
    _log.info("reconciler: expired private image %s pruned (object + row deleted)", row_id)
    return True
