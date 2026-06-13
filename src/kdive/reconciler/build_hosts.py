"""Build-host repair for the reconciler."""

from __future__ import annotations

import logging

from psycopg import AsyncConnection

_log = logging.getLogger(__name__)


async def reclaim_orphan_build_host_leases(conn: AsyncConnection) -> int:
    """Delete build-host leases whose owning BUILD job is terminal or gone.

    A lease is reclaimed when no BUILD job for its run_id is still live (queued/running).
    Keyed on job liveness, never on elapsed time, so a legitimately long-running build keeps
    its slot. Idempotent. Returns the number of leases reclaimed.

    The payload->>'run_id' extract is unindexed on the jobs side, but build_host_leases
    is expected to be small (at most max_concurrent rows per host), so the correlated
    subquery is evaluated only for the (few) live leases — acceptable scan cost.
    """
    async with conn.transaction():
        cur = await conn.execute(
            "DELETE FROM build_host_leases l "
            "WHERE NOT EXISTS ("
            "    SELECT 1 FROM jobs j"
            "    WHERE j.kind = 'build'"
            "      AND (j.payload->>'run_id')::uuid = l.run_id"
            "      AND j.state IN ('queued', 'running')"
            ")"
        )
    reclaimed = cur.rowcount
    if reclaimed:
        _log.info("reconciler: reclaimed %d orphaned build-host lease(s)", reclaimed)
    return reclaimed
