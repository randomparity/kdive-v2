"""Build-host repair for the reconciler."""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection

from kdive.providers.reaping import BuildVmReaper

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


async def _build_job_is_live(conn: AsyncConnection, run_id: UUID) -> bool:
    """Whether a queued/running BUILD job exists for ``run_id`` (the reap guard, never age)."""
    cur = await conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'build' AND (payload->>'run_id')::uuid = %s "
        "AND state IN ('queued', 'running') LIMIT 1",
        (run_id,),
    )
    return (await cur.fetchone()) is not None


async def reap_orphan_build_vms(conn: AsyncConnection, reaper: BuildVmReaper) -> int:
    """Reap ephemeral build VMs whose owning BUILD job is terminal or gone (ADR-0100).

    Mirrors :func:`reclaim_orphan_build_host_leases`'s job-liveness guard so a build running up
    to ``MAKE_TIMEOUT_S`` keeps its VM. A domain whose name does not encode a Run (``run_id`` is
    ``None``) is left alone — it cannot be confirmed dead. **Ordering:** the reconciler runs this
    BEFORE the lease reclaim, so a freed slot never coexists with a live leaked VM (§4.6).
    Returns the number of VMs reaped.
    """
    reaped = 0
    for vm in await reaper.list_build_vms():
        if vm.run_id is None or await _build_job_is_live(conn, vm.run_id):
            continue
        await reaper.delete_build_vm(vm.domain_name)
        reaped += 1
    if reaped:
        _log.info("reconciler: reaped %d leaked build VM(s)", reaped)
    return reaped
