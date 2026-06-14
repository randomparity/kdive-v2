"""Garbage-collection style reconciler repairs."""

from __future__ import annotations

import logging
from datetime import timedelta

from psycopg import AsyncConnection

from kdive.providers.console_hosting import CollectorRegistry
from kdive.providers.reaping import DumpVolumeReaper
from kdive.reconciler.allocations import has_active_capture_job
from kdive.reconciler.systems import gone_system_state_values

_log = logging.getLogger(__name__)

DEFAULT_IDEMPOTENCY_RETENTION = timedelta(days=7)
DEFAULT_DUMP_VOLUME_GRACE = timedelta(minutes=30)


async def gc_idempotency_keys(conn: AsyncConnection, retention: timedelta) -> int:
    """Delete ``idempotency_keys`` rows older than ``retention`` (ADR-0040)."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM idempotency_keys WHERE created_at < now() - %s", (retention,)
        )
        deleted = cur.rowcount
    if deleted:
        _log.info("reconciler: GC'd %d idempotency key(s) past retention", deleted)
    return deleted


async def reap_console_collectors(conn: AsyncConnection, registry: CollectorRegistry) -> int:
    """Finalize and drop console collectors for gone Systems (ADR-0095)."""
    held = registry.system_ids()
    if not held:
        return 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, state FROM systems WHERE id = ANY(%s)",
            (list(held),),
        )
        states = {row[0]: row[1] for row in await cur.fetchall()}
    reaped = 0
    gone_states = gone_system_state_values()
    for system_id in held:
        state = states.get(system_id)
        if state is not None and state not in gone_states:
            continue
        await registry.finalize_and_drop_async(system_id)
        reaped += 1
        _log.info("reconciler: console collector for gone system %s finalized + reaped", system_id)
    return reaped


async def reap_orphaned_dump_volumes(
    conn: AsyncConnection, reaper: DumpVolumeReaper, grace: timedelta
) -> int:
    """Delete host_dump volumes orphaned by a non-graceful worker/host crash (ADR-0094)."""
    volumes = await reaper.list_dump_volumes()
    if not volumes:
        return 0
    cutoff_epoch = await _now_epoch(conn) - grace.total_seconds()
    reaped = 0
    for volume in volumes:
        if volume.mtime_epoch_s >= cutoff_epoch:
            continue
        if volume.system_id is not None and await has_active_capture_job(conn, volume.system_id):
            continue
        try:
            await reaper.delete_dump_volume(volume.name)
        except Exception:  # noqa: BLE001 - one volume failure must not starve the rest
            _log.warning(
                "reconciler: deleting orphaned dump volume %s failed; retry next pass",
                volume.name,
                exc_info=True,
            )
            continue
        reaped += 1
        _log.info("reconciler: reaped orphaned host_dump volume %s", volume.name)
    return reaped


async def _now_epoch(conn: AsyncConnection) -> float:
    """The Postgres clock as epoch seconds."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT extract(epoch from now())")
        row = await cur.fetchone()
    return float(row[0]) if row is not None else 0.0
