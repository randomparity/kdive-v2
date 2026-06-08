"""Tests for the reconciler loop (ADR-0021, issue #12)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import cast
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.state import AllocationState, DebugSessionState, RunState, SystemState
from kdive.reconciler import loop
from kdive.reconciler.loop import (
    InfraReaper,
    NullReaper,
    Reconciler,
    ReconcileReport,
    reconcile_once,
)
from tests.reconciler.conftest import (
    FakeReaper,
    _FakeDomain,
    connect,
    run_repair,
    seed_debug_session,
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
        expired_allocations=5,
        orphaned_systems=1,
        abandoned_jobs=2,
        dead_sessions=3,
        leaked_domains=4,
        idempotency_keys_gc_count=6,
        failures=("abandoned_jobs",),
    )
    assert report.expired_allocations == 5
    assert report.orphaned_systems == 1
    assert report.idempotency_keys_gc_count == 6
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


def test_zombie_with_malformed_run_payload_still_dead_letters_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.RUNNING)
            job_id = await seed_running_job(
                seed,
                "dk-bad-run-zombie",
                payload={"run_id": "not-a-uuid"},
                lease_seconds=-60,
                attempt=3,
                max_attempts=3,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_abandoned_jobs)
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state, error_category FROM jobs WHERE id = %s", (job_id,)
            )
            job = await cur.fetchone()
            assert job is not None
            assert job[0] == "failed"
            assert job[1] == "lease_expired"
            cur = await check.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
            run = await cur.fetchone()
            assert run is not None and run[0] == "running"

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


def _detach(stale_after: timedelta):
    return lambda conn: loop._repair_dead_sessions(conn, stale_after)


def test_stale_live_session_detached(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            session_id = await seed_debug_session(
                seed, run_id, state=DebugSessionState.LIVE, heartbeat_ago=timedelta(hours=1)
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _detach(loop.DEFAULT_DEBUG_SESSION_STALE_AFTER))
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state FROM debug_sessions WHERE id = %s", (session_id,)
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == "detached"

    asyncio.run(_run())


def test_recent_heartbeat_session_not_touched(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            session_id = await seed_debug_session(
                seed, run_id, state=DebugSessionState.LIVE, heartbeat_ago=timedelta(seconds=1)
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _detach(loop.DEFAULT_DEBUG_SESSION_STALE_AFTER))
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state FROM debug_sessions WHERE id = %s", (session_id,)
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == "live"

    asyncio.run(_run())


def test_null_heartbeat_session_not_touched(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            session_id = await seed_debug_session(
                seed, run_id, state=DebugSessionState.LIVE, heartbeat_ago=None
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _detach(loop.DEFAULT_DEBUG_SESSION_STALE_AFTER))
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state FROM debug_sessions WHERE id = %s", (session_id,)
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == "live"  # NULL heartbeat never swept

    asyncio.run(_run())


def _reap(reaper: FakeReaper):
    return lambda conn: loop._repair_leaked_domains(conn, reaper)


def test_leaked_domain_with_no_row_is_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        reaper = FakeReaper(_FakeDomain(name="vm-leak", system_id=uuid4()))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 1
        assert reaper.destroyed == ["vm-leak"]

    asyncio.run(_run())


def test_domain_with_ready_row_not_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.READY)
        reaper = FakeReaper(_FakeDomain(name="vm-ready", system_id=system_id))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 0
        assert reaper.destroyed == []

    asyncio.run(_run())


def test_untagged_domain_not_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        reaper = FakeReaper(_FakeDomain(name="vm-untagged", system_id=None))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 0
        assert reaper.destroyed == []

    asyncio.run(_run())


def test_torn_down_row_without_teardown_job_is_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        reaper = FakeReaper(_FakeDomain(name="vm-leftover", system_id=system_id))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 1
        assert reaper.destroyed == ["vm-leftover"]

    asyncio.run(_run())


def test_torn_down_row_with_inflight_teardown_not_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
            await seed_running_job(
                seed,
                f"{system_id}:teardown",
                kind="teardown",
                payload={"system_id": str(system_id)},
                lease_seconds=300,
                attempt=1,
                max_attempts=3,
            )
        reaper = FakeReaper(_FakeDomain(name="vm-mid-teardown", system_id=system_id))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 0
        assert reaper.destroyed == []  # a live teardown is mid-destroy (guard b)

    asyncio.run(_run())


def test_leaked_domain_destroy_failure_does_not_strand_others(migrated_url: str) -> None:
    async def _run() -> None:
        reaper = FakeReaper(
            _FakeDomain(name="vm-bad", system_id=uuid4()),
            _FakeDomain(name="vm-good", system_id=uuid4()),
            fail_on=frozenset({"vm-bad"}),
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        # vm-bad's destroy raised but did not abort the pass; vm-good was still reaped.
        assert reaper.destroyed == ["vm-bad", "vm-good"]
        assert count == 1  # only the successful destroy is counted

    asyncio.run(_run())


def test_mid_provision_domain_not_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        # Headline acceptance: a provisioning row protects the domain (guard a),
        # independent of any provision job (which keys on allocation_id, not system_id).
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.PROVISIONING)
        reaper = FakeReaper(_FakeDomain(name="vm-provisioning", system_id=system_id))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 0
        assert reaper.destroyed == []

    asyncio.run(_run())


def test_reconcile_once_counts_a_mixed_pass(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )  # orphan
            await seed_running_job(
                seed, "dk-z", lease_seconds=-60, attempt=3, max_attempts=3
            )  # zombie
            sys2 = await seed_system(seed)
            run2 = await seed_run(seed, sys2)
            await seed_debug_session(
                seed, run2, state=DebugSessionState.LIVE, heartbeat_ago=timedelta(hours=1)
            )  # dead session
        reaper = FakeReaper(_FakeDomain(name="vm-leak", system_id=uuid4()))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, reaper)
        assert report == ReconcileReport(
            expired_allocations=0,
            orphaned_systems=1,
            abandoned_jobs=1,
            dead_sessions=1,
            leaked_domains=1,
            idempotency_keys_gc_count=0,
            failures=(),
        )
        assert reaper.destroyed == ["vm-leak"]

    asyncio.run(_run())


def test_reconcile_once_isolates_a_failing_repair(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await seed_debug_session(
                seed, run_id, state=DebugSessionState.LIVE, heartbeat_ago=timedelta(hours=1)
            )

        async def _boom(conn: object) -> int:
            raise RuntimeError("abandoned-jobs repair blew up")

        monkeypatch.setattr(loop, "_repair_abandoned_jobs", _boom)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper())
        assert report.dead_sessions == 1  # other repairs still ran
        assert report.failures == ("abandoned_jobs",)

    asyncio.run(_run())


def test_reconciler_run_survives_a_failing_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        stop = asyncio.Event()
        calls = 0

        async def _run_once(self: Reconciler) -> ReconcileReport:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient pass failure")
            stop.set()
            return ReconcileReport(0, 0, 0, 0, 0, 0, ())

        monkeypatch.setattr(Reconciler, "run_once", _run_once)
        # run_once is monkeypatched, so the pool is never used; a cast keeps ty happy.
        pool = cast(AsyncConnectionPool, object())
        reconciler = Reconciler(pool, NullReaper(), interval=timedelta(milliseconds=5))
        await asyncio.wait_for(reconciler.run(stop), timeout=2.0)
        assert calls == 2  # raised once, retried, then stopped

    asyncio.run(_run())
