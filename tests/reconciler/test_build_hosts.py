"""Tests for reconciler build-host lease reclaim (Task 12, ADR-0099).

A build_host_leases row whose owning BUILD job is terminal or gone is deleted so
the capacity slot frees — the backstop for a worker that died mid-build.  Keyed on
job liveness (queued/running), never on elapsed time.

Seeding uses autocommit connections; repair runs through a real non-autocommit pool
to exercise the transaction-nesting path (mirrors test_loop.py conventions).
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.providers.reaping import NullReaper
from kdive.reconciler import loop
from kdive.reconciler.build_hosts import reclaim_orphan_build_host_leases
from kdive.reconciler.loop import reconcile_once
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_system

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_ssh_build_host(conn: psycopg.AsyncConnection) -> UUID:
    """Insert a minimal ssh build_host; return its id."""
    host_id = uuid4()
    await conn.execute(
        "INSERT INTO build_hosts (id, name, kind, address, ssh_credential_ref, "
        "    workspace_root, max_concurrent) "
        "VALUES (%s, %s, 'ssh', %s, %s, %s, %s)",
        (host_id, f"host-{host_id}", "10.0.0.1", "cred-ref", "/build", 2),
    )
    return host_id


async def _seed_lease(conn: psycopg.AsyncConnection, run_id: UUID, build_host_id: UUID) -> None:
    """Insert a build_host_leases row for (run_id, build_host_id)."""
    await conn.execute(
        "INSERT INTO build_host_leases (run_id, build_host_id) VALUES (%s, %s)",
        (run_id, build_host_id),
    )


async def _seed_build_job(conn: psycopg.AsyncConnection, run_id: UUID, *, state: str) -> UUID:
    """Insert a build job for run_id with the given state; return its id."""
    job_id = uuid4()
    await conn.execute(
        "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, "
        "    authorizing, dedup_key) "
        "VALUES (%s, 'build', %s, %s, 1, 3, %s, %s)",
        (
            job_id,
            Jsonb({"run_id": str(run_id)}),
            state,
            Jsonb({"principal": "test", "agent_session": None, "project": "p"}),
            f"build:{run_id}",
        ),
    )
    return job_id


async def _lease_exists(conn: psycopg.AsyncConnection, run_id: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM build_host_leases WHERE run_id = %s", (run_id,))
    return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_queued_job_lease_not_reclaimed(migrated_url: str) -> None:
    """A lease whose build job is queued (still live) must NOT be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="queued")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_running_job_lease_not_reclaimed(migrated_url: str) -> None:
    """A lease whose build job is running (still live) must NOT be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="running")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_failed_job_lease_reclaimed(migrated_url: str) -> None:
    """A lease whose build job is failed (terminal) must be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="failed")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 1
        async with await connect(migrated_url) as check:
            assert not await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_succeeded_job_lease_reclaimed(migrated_url: str) -> None:
    """A lease whose build job succeeded (terminal) must be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="succeeded")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 1
        async with await connect(migrated_url) as check:
            assert not await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_canceled_job_lease_reclaimed(migrated_url: str) -> None:
    """A lease whose build job is canceled (terminal) must be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="canceled")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 1
        async with await connect(migrated_url) as check:
            assert not await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_no_job_row_lease_reclaimed(migrated_url: str) -> None:
    """A lease with no matching BUILD job row at all must be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            # intentionally no job row inserted
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 1
        async with await connect(migrated_url) as check:
            assert not await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_reclaim_is_idempotent(migrated_url: str) -> None:
    """Running the repair twice is safe; the second pass returns 0."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="failed")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, reclaim_orphan_build_host_leases)
            second = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert first == 1
        assert second == 0

    asyncio.run(_run())


def test_reconcile_once_reports_reclaimed_build_host_leases(migrated_url: str) -> None:
    """reconcile_once includes the reclaim count in its report."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="failed")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper())

        assert report.reclaimed_build_host_leases == 1

    asyncio.run(_run())


def test_reclaim_spec_registered_in_loop() -> None:
    """_reclaim_build_host_leases alias is present in the loop module's __all__."""
    assert "_reclaim_build_host_leases" in loop.__all__


# ---------------------------------------------------------------------------
# Ephemeral build-VM reaping (ADR-0100)
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from kdive.providers.reaping import BuildVm  # noqa: E402
from kdive.reconciler.build_hosts import reap_orphan_build_vms  # noqa: E402


class _FakeBuildVmReaper:
    """Records delete_build_vm calls; returns a canned list_build_vms result."""

    def __init__(self, vms: list[BuildVm]) -> None:
        self._vms = vms
        self.deleted: list[str] = []

    async def list_build_vms(self) -> list[BuildVm]:
        return list(self._vms)

    async def delete_build_vm(self, domain_name: str) -> None:
        self.deleted.append(domain_name)


def _build_vm(run_id: UUID) -> BuildVm:
    return BuildVm(domain_name=f"kdive-build-{run_id}", run_id=run_id)


def test_build_vm_reaped_when_build_job_terminal(migrated_url: str) -> None:
    """A build VM whose BUILD job is terminal (failed) is reaped."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="failed")
        reaper = _FakeBuildVmReaper([_build_vm(run_id)])

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: reap_orphan_build_vms(conn, reaper))

        assert count == 1
        assert reaper.deleted == [f"kdive-build-{run_id}"]

    asyncio.run(_run())


def test_build_vm_not_reaped_when_build_job_live(migrated_url: str) -> None:
    """A build VM whose BUILD job is still running is NOT reaped (no age-based reap)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="running")
        reaper = _FakeBuildVmReaper([_build_vm(run_id)])

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: reap_orphan_build_vms(conn, reaper))

        assert count == 0
        assert reaper.deleted == []

    asyncio.run(_run())


def test_build_vm_reaped_when_no_job_row(migrated_url: str) -> None:
    """A build VM with no matching BUILD job row at all is reaped (orphan)."""

    async def _run() -> None:
        run_id = uuid4()  # no run, no job
        reaper = _FakeBuildVmReaper([_build_vm(run_id)])

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: reap_orphan_build_vms(conn, reaper))

        assert count == 1
        assert reaper.deleted == [f"kdive-build-{run_id}"]

    asyncio.run(_run())


def test_build_vm_reap_runs_before_lease_reclaim_in_repair_plan() -> None:
    """The reaped_build_vms repair must precede reclaimed_build_host_leases (reap before reclaim).

    Freeing a lease slot before reaping the leaked VM would let a new build over-admit the host
    past max_concurrent while the leaked VM still runs (ADR-0100 §4.6).
    """
    plan = loop._repair_plan(
        reaper=NullReaper(),
        config=loop.ReconcileConfig(),
        image_publish_grace=timedelta(minutes=5),
    )
    names = [spec.name for spec in plan]
    assert "reaped_build_vms" in names
    assert "reclaimed_build_host_leases" in names
    assert names.index("reaped_build_vms") < names.index("reclaimed_build_host_leases")
    assert callable(loop._reclaim_build_host_leases)
