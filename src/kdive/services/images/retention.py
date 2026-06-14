"""Shared image retention policy and deletion helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.cursor_async import AsyncCursor
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from kdive.domain.models import ImageVisibility, ResourceKind
from kdive.domain.state import SystemState
from kdive.provider_components.artifacts import ObjectListing

_log = logging.getLogger(__name__)

_PRIVATE_VISIBILITY = ImageVisibility.PRIVATE.value
_TERMINAL_SYSTEM_STATES = (SystemState.TORN_DOWN, SystemState.FAILED)
_TERMINAL_SYSTEM_STATE_VALUES = tuple(state.value for state in _TERMINAL_SYSTEM_STATES)
# A local-libvirt System references a catalog rootfs by (provider, name) under its provider
# section; a remote-libvirt System references its operator-staged base image by the volume
# name under its section (ADR-0080 ``base_image_volume``). The reference guard probes both so
# inventory prune (ADR-0112) never deletes an in-use base image of EITHER kind — a stale guard
# that missed the remote shape would let prune delete the row of a live remote base image,
# after which ``repair_leaked_images`` would reclaim its S3 object (deferred data loss).
_LOCAL_LIBVIRT_SECTION = ResourceKind.LOCAL_LIBVIRT.value
_REMOTE_LIBVIRT_SECTION = ResourceKind.REMOTE_LIBVIRT.value


@runtime_checkable
class ImageSweepStore(Protocol):
    """The narrow object-store port the image sweeps consume."""

    def list_image_objects(self) -> list[ObjectListing]: ...
    def head_present(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


async def repair_expired_private_images(conn: AsyncConnection, store: ImageSweepStore) -> int:
    """Delete expired private images whose retention guards allow pruning.

    Candidates are ``private`` rows with ``expires_at < now()``. Each row is rechecked under
    :func:`expire_one_private_image`, which holds the row lock while honoring both the
    non-terminal-System reference guard and the concurrent-extend fence.
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
        if await expire_one_private_image(conn, store, cand["id"], cand["object_key"]):
            pruned += 1
    return pruned


async def image_referenced_by_live_system(cur: AsyncCursor[DictRow], row_id: UUID) -> bool:
    """Return whether a non-terminal System references this image as its base.

    Covers both reference shapes (ADR-0112 prune guard):

    * **local-libvirt** — a ``catalog`` rootfs naming the image's ``(provider, name)``;
    * **remote-libvirt** — a ``base_image_volume`` naming the image's staged ``volume``
      (ADR-0080); only checked when the image carries a ``volume`` (a staged image).

    A non-terminal System matching **either** shape returns ``True`` so prune cordons rather
    than deletes the in-use base image.
    """
    await cur.execute("SELECT provider, name, volume FROM image_catalog WHERE id = %s", (row_id,))
    image = await cur.fetchone()
    if image is None:
        return False
    probes = [
        Jsonb(
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
    ]
    if image["volume"] is not None:
        probes.append(
            Jsonb({"provider": {_REMOTE_LIBVIRT_SECTION: {"base_image_volume": image["volume"]}}})
        )
    for probe in probes:
        await cur.execute(
            "SELECT 1 FROM systems WHERE state <> ALL(%s) AND provisioning_profile @> %s LIMIT 1",
            (list(_TERMINAL_SYSTEM_STATE_VALUES), probe),
        )
        if await cur.fetchone() is not None:
            return True
    return False


async def expire_one_private_image(
    conn: AsyncConnection, store: ImageSweepStore, row_id: UUID, object_key: str | None
) -> bool:
    """Delete one expired private image's object and row if no retention guard blocks it.

    The locked ``expires_at < now()`` re-read is the extend fence: a concurrent operator extend
    committed after candidate selection turns this into a no-op. The reference guard runs under
    the same transaction so a System that still uses the image defers expiry. Object deletion
    precedes row deletion so a crash leaves at most a dangling row for the reconciler to heal.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT 1 FROM image_catalog "
            "WHERE id = %s AND visibility = %s "
            "  AND expires_at IS NOT NULL AND expires_at < now() FOR UPDATE",
            (row_id, _PRIVATE_VISIBILITY),
        )
        if await cur.fetchone() is None:
            return False
        if await image_referenced_by_live_system(cur, row_id):
            _log.info(
                "images: expired private image %s referenced by a non-terminal System; "
                "deferring expiry",
                row_id,
            )
            return False
        if object_key is not None:
            await asyncio.to_thread(store.delete, object_key)
        await cur.execute("DELETE FROM image_catalog WHERE id = %s", (row_id,))
    _log.info("images: expired private image %s pruned (object + row deleted)", row_id)
    return True
