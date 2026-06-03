"""Tests for the reconciler loop (ADR-0021, issue #12)."""

from __future__ import annotations

import asyncio

from psycopg_pool import AsyncConnectionPool

from kdive.domain.state import AllocationState, RunState, SystemState
from kdive.reconciler import loop
from kdive.reconciler.loop import InfraReaper, NullReaper, ReconcileReport
from tests.reconciler.conftest import (
    connect,
    run_repair,
    seed_run,
    seed_running_job,
    seed_system,
)


def test_null_reaper_is_an_infra_reaper() -> None:
    assert isinstance(NullReaper(), InfraReaper)


def test_null_reaper_lists_nothing_and_destroy_is_noop() -> None:
    async def _run() -> None:
        reaper = NullReaper()
        assert await reaper.list_owned() == []
        assert await reaper.destroy("anything") is None

    asyncio.run(_run())


def test_reconcile_report_holds_counts_and_failures() -> None:
    report = ReconcileReport(
        orphaned_systems=1,
        abandoned_jobs=2,
        dead_sessions=3,
        leaked_domains=4,
        failures=("abandoned_jobs",),
    )
    assert report.orphaned_systems == 1
    assert report.failures == ("abandoned_jobs",)


def test_orphaned_system_enqueues_gc_teardown(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_orphaned_systems)
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
            row = await cur.fetchone()
            assert row is not None and row[0] == "ready"  # System untouched
            cur = await check.execute(
                "SELECT kind, authorizing FROM jobs WHERE dedup_key = %s",
                (f"{system_id}:teardown",),
            )
            job = await cur.fetchone()
            assert job is not None
            assert job[0] == "teardown"
            assert job[1]["principal"] == "system:reconciler"  # GC attribution

    asyncio.run(_run())


def test_orphaned_system_second_pass_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.FAILED
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, loop._repair_orphaned_systems)
            second = await run_repair(pool, loop._repair_orphaned_systems)
        assert first == 1
        assert second == 0  # already queued: a re-pass enqueues nothing new
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT count(*) FROM jobs WHERE kind = 'teardown'")
            row = await cur.fetchone()
            assert row is not None and row[0] == 1  # exactly one job

    asyncio.run(_run())


def test_active_allocation_system_not_touched(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.ACTIVE
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_orphaned_systems)
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT count(*) FROM jobs")
            row = await cur.fetchone()
            assert row is not None and row[0] == 0

    asyncio.run(_run())


def test_terminal_system_on_released_allocation_not_touched(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.TORN_DOWN, alloc_state=AllocationState.RELEASED
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_orphaned_systems)
        assert count == 0

    asyncio.run(_run())


def test_zombie_job_dead_lettered_with_lease_expired(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            job_id = await seed_running_job(
                seed, "dk-zombie", lease_seconds=-60, attempt=3, max_attempts=3
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_abandoned_jobs)
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state, error_category FROM jobs WHERE id = %s", (job_id,)
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == "failed"
            assert row[1] == "lease_expired"

    asyncio.run(_run())


def test_zombie_job_compensates_owning_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.RUNNING)
            await seed_running_job(
                seed,
                "dk-run-zombie",
                payload={"run_id": str(run_id)},
                lease_seconds=-60,
                attempt=3,
                max_attempts=3,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            await run_repair(pool, loop._repair_abandoned_jobs)
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state, failure_category FROM runs WHERE id = %s", (run_id,)
            )
            row = await cur.fetchone()
            assert row is not None
            assert row[0] == "failed"
            assert row[1] == "lease_expired"

    asyncio.run(_run())


def test_zombie_without_run_id_leaves_runs_untouched(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.RUNNING)
            await seed_running_job(
                seed,
                "dk-sys-zombie",
                payload={"system_id": str(system_id)},
                lease_seconds=-60,
                attempt=3,
                max_attempts=3,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            await run_repair(pool, loop._repair_abandoned_jobs)
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
            row = await cur.fetchone()
            assert row is not None and row[0] == "running"  # untouched

    asyncio.run(_run())


def test_live_lease_and_attempts_remaining_not_swept(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_running_job(
                seed, "dk-live", lease_seconds=300, attempt=3, max_attempts=3
            )  # future lease
            await seed_running_job(
                seed, "dk-retryable", lease_seconds=-60, attempt=1, max_attempts=3
            )  # lapsed but attempts remain -> dequeue's job, not the reconciler's
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_abandoned_jobs)
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT count(*) FROM jobs WHERE state = 'failed'")
            row = await cur.fetchone()
            assert row is not None and row[0] == 0

    asyncio.run(_run())
