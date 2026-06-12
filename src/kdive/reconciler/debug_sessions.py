"""Debug-session repair for the reconciler."""

from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.state import DebugSessionState
from kdive.providers.transport_reset import TransportResetter

_log = logging.getLogger(__name__)

_DETACHED_DEBUG_SESSION_STATE_VALUE = DebugSessionState.DETACHED.value
_LIVE_DEBUG_SESSION_STATE_VALUE = DebugSessionState.LIVE.value


async def repair_dead_sessions(
    conn: AsyncConnection, stale_after: timedelta, resetter: TransportResetter
) -> int:
    """Detach stale ``live`` debug sessions, then reset each dead transport best-effort."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE debug_sessions SET state = %s "
            "WHERE state = %s AND worker_heartbeat_at IS NOT NULL "
            "  AND worker_heartbeat_at < now() - %s "
            "RETURNING id, run_id, transport, transport_handle",
            (_DETACHED_DEBUG_SESSION_STATE_VALUE, _LIVE_DEBUG_SESSION_STATE_VALUE, stale_after),
        )
        rows = await cur.fetchall()
    for row in rows:
        _log.info("reconciler: dead debug_session %s -> detached", row["id"])
        await _reset_dead_transport(conn, resetter, row)
    return len(rows)


async def _reset_dead_transport(
    conn: AsyncConnection, resetter: TransportResetter, row: dict
) -> None:
    """Reset one detached session's transport, guarded and best-effort (ADR-0086)."""
    async with conn.transaction():
        system_id, domain_name = await _resolve_system(conn, row["run_id"])
        has_live_holder = system_id is not None and await _has_live_gdbstub_holder(conn, system_id)
    if has_live_holder:
        _log.info(
            "reconciler: session %s detached but System %s has a live gdbstub holder; "
            "skipping transport reset",
            row["id"],
            system_id,
        )
        return
    try:
        await resetter.reset(
            transport=row["transport"],
            transport_handle=row["transport_handle"],
            domain_name=domain_name,
        )
    except Exception:  # noqa: BLE001 - one reset failure must not starve the sweep
        _log.warning(
            "reconciler: resetting dead transport for session %s failed; the next attach "
            "may contend (transport_conflict)",
            row["id"],
            exc_info=True,
        )


async def _resolve_system(conn: AsyncConnection, run_id: UUID) -> tuple[UUID | None, str | None]:
    """Return ``(system_id, domain_name)`` for a Run, or ``(None, None)`` if gone."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id AS system_id, s.domain_name "
            "FROM runs r JOIN systems s ON s.id = r.system_id WHERE r.id = %s",
            (run_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None, None
    return row["system_id"], row["domain_name"]


async def _has_live_gdbstub_holder(conn: AsyncConnection, system_id: UUID) -> bool:
    """True if ``system_id`` currently has a live gdbstub holder."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM debug_sessions ds JOIN runs r ON r.id = ds.run_id "
            "WHERE r.system_id = %s AND ds.state = %s AND ds.transport = %s LIMIT 1",
            (system_id, _LIVE_DEBUG_SESSION_STATE_VALUE, "gdbstub"),
        )
        return await cur.fetchone() is not None
