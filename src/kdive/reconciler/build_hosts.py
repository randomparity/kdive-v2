"""Build-host repair for the reconciler."""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.build_hosts import BuildHostState, list_probeable_ssh_hosts, mark_state
from kdive.providers.build_host.reachability import BuildHostProber
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


async def probe_build_host_reachability(conn: AsyncConnection, prober: BuildHostProber) -> int:
    """Probe each enabled SSH build host and CAS-flip its state ready↔unreachable (ADR-0103).

    The probe set (``kind='ssh' AND enabled=true``) is read in a committed transaction
    first, so no transaction is held open across the per-host network probe. Each host is
    probed via ``prober`` (a bare ``ssh … true``); the result is written with a
    compare-and-swap on the observed state, each in its own committed transaction, so a
    concurrent operator change is never clobbered and a no-op probe writes nothing. One
    host's unexpected failure is logged and skipped — it never aborts the pass.

    Returns the number of state **transitions** written (``0`` in a healthy steady state).
    A non-empty pass also logs the probed-vs-flipped counts at ``info`` so "the probe ran"
    is observable independent of whether any host flipped.
    """
    async with conn.transaction():
        hosts = await list_probeable_ssh_hosts(conn)
    if not hosts:
        return 0

    changed = 0
    for host in hosts:
        try:
            reachable = await prober.probe(host)
            new_state = BuildHostState.READY if reachable else BuildHostState.UNREACHABLE
            if new_state != host.state:
                async with conn.transaction():
                    changed += await mark_state(
                        conn, host.id, new_state=new_state, expected_state=host.state
                    )
        except Exception:  # noqa: BLE001 - isolate one host; a bad probe must not starve the rest
            _log.warning("reconciler: probing build host %r failed this pass; skipping", host.name)
    _log.info("reconciler: probed %d ssh build host(s); %d state change(s)", len(hosts), changed)
    return changed


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
