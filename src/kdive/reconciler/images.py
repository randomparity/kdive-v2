"""Image-catalog object drift repair for the reconciler (M2.4/6, ADR-0092, ADR-0093).

Two deadline-guarded sweeps, modeled on :func:`kdive.reconciler.uploads.repair_abandoned_uploads`
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
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.models import ImageState
from kdive.provider_components.artifacts import ObjectListing
from kdive.services.images.retention import ImageSweepStore

_log = logging.getLogger(__name__)

_DEFINED_STATE = ImageState.DEFINED.value


# The image sweeps consume the shared object-listing value type (key + store mtime); the
# alias keeps the reconciler tests' import surface stable while reusing one definition.
ImageMtime = ObjectListing


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
