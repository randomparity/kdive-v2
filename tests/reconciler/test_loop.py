"""Tests for the reconciler loop (ADR-0021, issue #12)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.state import AllocationState, DebugSessionState, RunState, SystemState
from kdive.providers.reaping import DumpVolume, InfraReaper, NullReaper
from kdive.reconciler import loop
from kdive.reconciler.debug_sessions import repair_dead_sessions
from kdive.reconciler.gc import (
    reap_console_collectors,
    reap_orphaned_dump_volumes,
)
from kdive.reconciler.jobs import repair_abandoned_jobs
from kdive.reconciler.loop import (
    ReconcileConfig,
    Reconciler,
    ReconcileReport,
    reconcile_once,
)
from kdive.reconciler.provider_reaping import repair_leaked_domains
from kdive.reconciler.systems import repair_orphaned_systems
from tests.reconciler.conftest import (
    FakeReaper,
    FakeResetter,
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
            count = await run_repair(pool, repair_orphaned_systems)
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
            first = await run_repair(pool, repair_orphaned_systems)
            second = await run_repair(pool, repair_orphaned_systems)
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
            count = await run_repair(pool, repair_orphaned_systems)
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
            count = await run_repair(pool, repair_orphaned_systems)
        assert count == 0

    asyncio.run(_run())


def test_zombie_job_dead_lettered_with_lease_expired(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            job_id = await seed_running_job(
                seed, "dk-zombie", lease_seconds=-60, attempt=3, max_attempts=3
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, repair_abandoned_jobs)
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
            await run_repair(pool, repair_abandoned_jobs)
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
            await run_repair(pool, repair_abandoned_jobs)
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
            count = await run_repair(pool, repair_abandoned_jobs)
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
            count = await run_repair(pool, repair_abandoned_jobs)
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT count(*) FROM jobs WHERE state = 'failed'")
            row = await cur.fetchone()
            assert row is not None and row[0] == 0

    asyncio.run(_run())


def _detach(stale_after: timedelta, resetter=None):
    from kdive.providers.transport_reset import NullResetter

    r = resetter if resetter is not None else NullResetter()
    return lambda conn: repair_dead_sessions(conn, stale_after, r)


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


def test_stale_gdbstub_session_triggers_a_reset(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            await seed.execute(
                "UPDATE systems SET domain_name = %s WHERE id = %s", ("kdive-sys", system_id)
            )
            run_id = await seed_run(seed, system_id)
            await seed_debug_session(
                seed,
                run_id,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(hours=1),
                transport="gdbstub",
                transport_handle="gdbstub://10.0.0.5:1234",
            )
        resetter = FakeResetter()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool, _detach(loop.DEFAULT_DEBUG_SESSION_STALE_AFTER, resetter)
            )
        assert count == 1
        assert resetter.calls == [
            {
                "transport": "gdbstub",
                "transport_handle": "gdbstub://10.0.0.5:1234",
                "domain_name": "kdive-sys",
            }
        ]

    asyncio.run(_run())


def test_live_holder_guard_skips_reset(migrated_url: str) -> None:
    """A System with a fresh live gdbstub session is not reset (no eviction)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            await seed.execute(
                "UPDATE systems SET domain_name = %s WHERE id = %s", ("kdive-sys", system_id)
            )
            stale_run = await seed_run(seed, system_id)
            await seed_debug_session(
                seed,
                stale_run,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(hours=1),
                transport="gdbstub",
                transport_handle="gdbstub://10.0.0.5:1234",
            )
            fresh_run = await seed_run(seed, system_id)
            await seed_debug_session(
                seed,
                fresh_run,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(seconds=1),
                transport="gdbstub",
                transport_handle="gdbstub://10.0.0.5:1234",
            )
        resetter = FakeResetter()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool, _detach(loop.DEFAULT_DEBUG_SESSION_STALE_AFTER, resetter)
            )
        assert count == 1  # the one stale session is still detached
        assert resetter.calls == []  # but the fresh live gdbstub holder => reset skipped

    asyncio.run(_run())


def test_reset_failure_does_not_strand_the_detach(migrated_url: str) -> None:
    """A raising resetter is swallowed; the session is still detached and the count stands."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            await seed.execute(
                "UPDATE systems SET domain_name = %s WHERE id = %s", ("kdive-sys", system_id)
            )
            run_id = await seed_run(seed, system_id)
            session_id = await seed_debug_session(
                seed,
                run_id,
                state=DebugSessionState.LIVE,
                heartbeat_ago=timedelta(hours=1),
                transport="gdbstub",
                transport_handle="gdbstub://10.0.0.5:1234",
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool, _detach(loop.DEFAULT_DEBUG_SESSION_STALE_AFTER, FakeResetter(fail=True))
            )
        assert count == 1  # the reset raised but the detach stands
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state FROM debug_sessions WHERE id = %s", (session_id,)
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == "detached"

    asyncio.run(_run())


def _reap(reaper: FakeReaper):
    return lambda conn: repair_leaked_domains(conn, reaper)


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


def test_name_orphaned_domain_with_no_row_is_reaped(migrated_url: str) -> None:
    # #372: a kdive-<uuid> domain with no metadata tag (system_id=None) and no DB row is a
    # genuine orphan; the name resolves the owning System so the sweep reaps it.
    async def _run() -> None:
        sid = uuid4()
        reaper = FakeReaper(_FakeDomain(name=f"kdive-{sid}", system_id=None))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 1
        assert reaper.destroyed == [f"kdive-{sid}"]

    asyncio.run(_run())


def test_foreign_domain_with_no_row_not_reaped(migrated_url: str) -> None:
    # #372 safety: a name that does not match kdive-<uuid> is foreign/unmanaged and untouched,
    # even with no DB row backing it.
    async def _run() -> None:
        reaper = FakeReaper(_FakeDomain(name="someone-elses-vm", system_id=None))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 0
        assert reaper.destroyed == []

    asyncio.run(_run())


def test_name_orphan_with_live_provisioning_row_is_preserved(migrated_url: str) -> None:
    # #372 mid-creation guard: a live (state <> torn_down) systems row for the name-resolved
    # id protects the domain — a System mid-creation is never reaped.
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            sid = await seed_system(seed, system_state=SystemState.PROVISIONING)
        reaper = FakeReaper(_FakeDomain(name=f"kdive-{sid}", system_id=None))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        assert count == 0
        assert reaper.destroyed == []

    asyncio.run(_run())


def test_metadata_tag_wins_over_domain_name_for_resolution(migrated_url: str) -> None:
    # #372: when a domain carries a metadata tag, it stays authoritative — the guards apply to
    # the tagged System (B), not the System encoded in the name (A).
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            sid_b = await seed_system(seed, system_state=SystemState.READY)
        sid_a = uuid4()
        reaper = FakeReaper(_FakeDomain(name=f"kdive-{sid_a}", system_id=sid_b))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, _reap(reaper))
        # B has a live ready row → protected; A's name is ignored because the tag wins.
        assert count == 0
        assert reaper.destroyed == []

    asyncio.run(_run())


def test_name_orphan_reaping_is_idempotent(migrated_url: str) -> None:
    # #372: a second pass with the domain gone reaps nothing.
    async def _run() -> None:
        sid = uuid4()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = FakeReaper(_FakeDomain(name=f"kdive-{sid}", system_id=None))
            assert await run_repair(pool, _reap(first)) == 1
            second = FakeReaper()  # domain gone → provider lists nothing
            assert await run_repair(pool, _reap(second)) == 0
            assert second.destroyed == []

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
        reconciler = Reconciler(
            pool,
            NullReaper(),
            config=ReconcileConfig(interval=timedelta(milliseconds=5)),
        )
        await asyncio.wait_for(reconciler.run(stop), timeout=2.0)
        assert calls == 2  # raised once, retried, then stopped

    asyncio.run(_run())


def test_reconciler_run_wakes_promptly_when_stopped_during_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        stop = asyncio.Event()
        first_pass_done = asyncio.Event()

        async def _run_once(self: Reconciler) -> ReconcileReport:
            first_pass_done.set()
            return ReconcileReport(0, 0, 0, 0, 0, 0, ())

        monkeypatch.setattr(Reconciler, "run_once", _run_once)
        pool = cast(AsyncConnectionPool, object())
        reconciler = Reconciler(
            pool,
            NullReaper(),
            config=ReconcileConfig(interval=timedelta(seconds=30)),
        )
        task = asyncio.create_task(reconciler.run(stop))
        await asyncio.wait_for(first_pass_done.wait(), timeout=1.0)

        stop.set()

        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_run())


# --- console collector reap (ADR-0095, #303) -----------------------------------------------


class _FakeConsoleCollector:
    """A console collector double for the reap class: records finalize/close."""

    def __init__(self, system_id: UUID) -> None:
        self.system_id = system_id
        self.finalized = False
        self.closed = False

    def pump_once(self) -> bool:
        return True

    def finalize(self) -> None:
        self.finalized = True

    def close(self) -> None:
        self.closed = True


def test_console_reap_finalizes_and_drops_gone_system(migrated_url: str) -> None:
    from kdive.providers.console_hosting import CollectorRegistry

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            gone = await seed_system(seed, system_state=SystemState.TORN_DOWN)
        registry = CollectorRegistry()
        collector = _FakeConsoleCollector(gone)
        registry.add(collector)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: reap_console_collectors(c, registry))
        assert count == 1
        # Finalize ran (artifact persisted) BEFORE the drop — reap never races finalize (AC7).
        assert collector.finalized is True
        assert registry.has(gone) is False

    asyncio.run(_run())


def test_console_reap_leaves_live_system_collector(migrated_url: str) -> None:
    from kdive.providers.console_hosting import CollectorRegistry

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            live = await seed_system(seed, system_state=SystemState.READY)
        registry = CollectorRegistry()
        collector = _FakeConsoleCollector(live)
        registry.add(collector)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: reap_console_collectors(c, registry))
        assert count == 0
        assert collector.finalized is False  # a live System keeps streaming
        assert registry.has(live) is True

    asyncio.run(_run())


def test_console_reap_drops_vanished_system_collector(migrated_url: str) -> None:
    # A System row deleted out from under the collector (no row at all) is "gone" and reaped.
    from kdive.providers.console_hosting import CollectorRegistry

    async def _run() -> None:
        registry = CollectorRegistry()
        vanished = uuid4()
        collector = _FakeConsoleCollector(vanished)
        registry.add(collector)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: reap_console_collectors(c, registry))
        assert count == 1
        assert collector.finalized is True
        assert registry.has(vanished) is False

    asyncio.run(_run())


def test_console_reap_with_empty_registry_is_noop(migrated_url: str) -> None:
    # A non-leader replica hosts no collectors (AC5): the reap class touches nothing.
    from kdive.providers.console_hosting import CollectorRegistry

    async def _run() -> None:
        registry = CollectorRegistry()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: reap_console_collectors(c, registry))
        assert count == 0

    asyncio.run(_run())


# --- host_dump orphaned-volume reap (ADR-0094, #301) ---------------------------------------


class _FakeDumpVolumeReaper:
    """Records delete calls; returns scripted volumes; structurally a DumpVolumeReaper."""

    def __init__(self, *volumes: DumpVolume, fail_on: frozenset[str] = frozenset()) -> None:
        self._volumes = list(volumes)
        self._fail_on = fail_on
        self.deleted: list[str] = []

    async def list_dump_volumes(self) -> list[DumpVolume]:
        return list(self._volumes)

    async def delete_dump_volume(self, name: str) -> None:
        self.deleted.append(name)
        if name in self._fail_on:
            raise RuntimeError(f"libvirt vol delete of {name} failed")


def test_null_dump_volume_reaper_is_a_dump_volume_reaper() -> None:
    from kdive.providers.reaping import DumpVolumeReaper, NullDumpVolumeReaper

    async def _run() -> None:
        reaper = NullDumpVolumeReaper()
        assert isinstance(reaper, DumpVolumeReaper)
        assert await reaper.list_dump_volumes() == []
        assert await reaper.delete_dump_volume("anything") is None

    asyncio.run(_run())


async def _seed_capture_job(conn: psycopg.AsyncConnection, system_id: UUID, *, state: str) -> None:
    from psycopg.types.json import Jsonb

    await conn.execute(
        "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, authorizing, dedup_key) "
        "VALUES ('capture_vmcore', %s, %s, 0, 5, %s, %s)",
        (
            Jsonb({"system_id": str(system_id), "method": "host_dump"}),
            state,
            Jsonb({"principal": "p", "agent_session": None, "project": "proj"}),
            f"{system_id}:capture_vmcore:host_dump:{state}",
        ),
    )


async def _seed_now_epoch(conn: psycopg.AsyncConnection) -> float:
    cur = await conn.execute("SELECT extract(epoch from now())")
    row = await cur.fetchone()
    assert row is not None
    return float(row[0])


def _vol(system_id: UUID | None, *, age_s: float, now_epoch: float) -> DumpVolume:
    suffix = str(system_id) if system_id is not None else "stray"
    return DumpVolume(
        name=f"kdive-host-dump-{suffix}.kdump",
        system_id=system_id,
        mtime_epoch_s=now_epoch - age_s,
    )


def test_reaps_old_orphan_without_active_capture(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.CRASHED)
            now_epoch = await _seed_now_epoch(seed)
        reaper = _FakeDumpVolumeReaper(_vol(system_id, age_s=3600, now_epoch=now_epoch))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool,
                lambda conn: reap_orphaned_dump_volumes(conn, reaper, timedelta(minutes=30)),
            )
        assert count == 1
        assert reaper.deleted == [f"kdive-host-dump-{system_id}.kdump"]

    asyncio.run(_run())


def test_does_not_reap_a_volume_younger_than_the_grace_window(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.CRASHED)
            now_epoch = await _seed_now_epoch(seed)
        # 60s old, grace 30m: a fresh volume a live capture may still be writing.
        reaper = _FakeDumpVolumeReaper(_vol(system_id, age_s=60, now_epoch=now_epoch))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool,
                lambda conn: reap_orphaned_dump_volumes(conn, reaper, timedelta(minutes=30)),
            )
        assert count == 0
        assert reaper.deleted == []  # live-holder guard #1 (mtime)

    asyncio.run(_run())


def test_does_not_reap_a_volume_with_an_active_capture_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.CRASHED)
            await _seed_capture_job(seed, system_id, state="running")
            now_epoch = await _seed_now_epoch(seed)
        # Old enough to clear the mtime guard, but a running capture holds it live.
        reaper = _FakeDumpVolumeReaper(_vol(system_id, age_s=3600, now_epoch=now_epoch))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool,
                lambda conn: reap_orphaned_dump_volumes(conn, reaper, timedelta(minutes=30)),
            )
        assert count == 0
        assert reaper.deleted == []  # live-holder guard #2 (active capture job)

    asyncio.run(_run())


def test_reaps_when_capture_job_is_terminal(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.CRASHED)
            await _seed_capture_job(seed, system_id, state="succeeded")
            now_epoch = await _seed_now_epoch(seed)
        reaper = _FakeDumpVolumeReaper(_vol(system_id, age_s=3600, now_epoch=now_epoch))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool,
                lambda conn: reap_orphaned_dump_volumes(conn, reaper, timedelta(minutes=30)),
            )
        assert count == 1  # a finished capture no longer holds the volume

    asyncio.run(_run())


def test_one_volume_delete_failure_does_not_starve_the_rest(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            sid_a = await seed_system(seed, system_state=SystemState.CRASHED)
            sid_b = await seed_system(seed, system_state=SystemState.CRASHED)
            now_epoch = await _seed_now_epoch(seed)
        reaper = _FakeDumpVolumeReaper(
            _vol(sid_a, age_s=3600, now_epoch=now_epoch),
            _vol(sid_b, age_s=3600, now_epoch=now_epoch),
            fail_on=frozenset({f"kdive-host-dump-{sid_a}.kdump"}),
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool,
                lambda conn: reap_orphaned_dump_volumes(conn, reaper, timedelta(minutes=30)),
            )
        assert count == 1  # a's delete raised; b still got reaped
        assert len(reaper.deleted) == 2  # both attempted

    asyncio.run(_run())


def test_reaps_a_stray_named_volume_with_no_system(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            now_epoch = await _seed_now_epoch(seed)
        reaper = _FakeDumpVolumeReaper(_vol(None, age_s=3600, now_epoch=now_epoch))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool,
                lambda conn: reap_orphaned_dump_volumes(conn, reaper, timedelta(minutes=30)),
            )
        assert count == 1  # no System => no live capture possible => age-reap

    asyncio.run(_run())
