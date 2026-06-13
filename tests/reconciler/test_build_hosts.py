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
    assert callable(loop._reclaim_build_host_leases)
