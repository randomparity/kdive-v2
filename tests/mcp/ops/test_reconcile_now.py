"""``ops.reconcile_now`` handler tests (#137, ADR-0062 §reconcile).

The handler is called directly with an injected pool and request context (the repo's
primary test contract). These tests prove the three acceptance criteria:

* a pending repair (an orphaned System) is resolved by one call, which returns a
  per-class summary, and the periodic loop's machinery is untouched;
* the on-demand pass shares the periodic reconciler's ``reconcile_once`` and its
  per-System advisory lock, so an on-demand pass and a concurrent periodic pass
  serialize on the same lock and cannot double-act on one object;
* ``platform_operator`` gating is enforced (a non-operator is denied and writes no
  ``platform_audit_log`` row).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.state import AllocationState, SystemState
from kdive.mcp.tools.ops import reconcile as ops_reconcile
from kdive.reconciler.loop import NullReaper, reconcile_once
from kdive.security.context import RequestContext
from kdive.security.rbac import PlatformRole
from tests.reconciler.conftest import connect, seed_system


def _ctx(*, platform_roles: frozenset[PlatformRole] = frozenset()) -> RequestContext:
    return RequestContext(
        principal="op-1",
        agent_session="sess-1",
        projects=(),
        roles={},
        platform_roles=platform_roles,
    )


_OPERATOR = frozenset({PlatformRole.PLATFORM_OPERATOR})


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=5, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _teardown_job_count(url: str) -> int:
    async with await connect(url) as check:
        cur = await check.execute("SELECT count(*) FROM jobs WHERE kind = 'teardown'")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _platform_audit_count(url: str) -> int:
    async with await connect(url) as check:
        cur = await check.execute(
            "SELECT count(*) FROM platform_audit_log WHERE tool = 'ops.reconcile_now'"
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    async with await connect(url) as check:
        cur = await check.execute(
            "SELECT principal, platform_role, scope FROM platform_audit_log "
            "WHERE tool = 'ops.reconcile_now'"
        )
        return await cur.fetchall()


def test_reconcile_now_resolves_orphaned_system_and_returns_summary(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(
                pool, _ctx(platform_roles=_OPERATOR), reaper=NullReaper(), upload_store=None
            )
        assert resp.status == "ok"
        assert resp.data["orphaned_systems"] == "1"
        assert resp.data["failures"] == ""
        # The pending repair was actually performed, not just counted.
        assert await _teardown_job_count(migrated_url) == 1
        # The action was audited to platform_audit_log with the caller's held roles.
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_operator", "all-projects")]

    asyncio.run(_run())


def test_audit_records_all_held_platform_roles(migrated_url: str) -> None:
    # The audit row reflects the roles the caller actually holds, not the gate literal —
    # an operator who also holds auditor records both (sorted, comma-joined).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(
                pool,
                _ctx(
                    platform_roles=frozenset(
                        {PlatformRole.PLATFORM_OPERATOR, PlatformRole.PLATFORM_AUDITOR}
                    )
                ),
                reaper=NullReaper(),
                upload_store=None,
            )
        assert resp.status == "ok"
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_auditor,platform_operator", "all-projects")]

    asyncio.run(_run())


def test_reconcile_now_clean_state_returns_zero_summary(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(
                pool, _ctx(platform_roles=_OPERATOR), reaper=NullReaper(), upload_store=None
            )
        assert resp.status == "ok"
        assert resp.data["orphaned_systems"] == "0"
        assert resp.data["expired_allocations"] == "0"
        assert resp.data["failures"] == ""
        # A pass with nothing to repair is still audited (it ran a control action).
        assert await _platform_audit_count(migrated_url) == 1

    asyncio.run(_run())


def test_project_only_non_operator_is_denied_and_writes_no_audit_row(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(
                pool, _ctx(), reaper=NullReaper(), upload_store=None
            )
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
            assert resp.suggested_next_actions == ["ops.reconcile_now"]
        # The denied calls performed no repair and wrote no audit row.
        assert await _teardown_job_count(migrated_url) == 0
        assert await _platform_audit_count(migrated_url) == 0

    asyncio.run(_run())


def test_auditor_non_operator_denial_is_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(
                pool,
                _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR})),
                reaper=NullReaper(),
                upload_store=None,
            )
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
        assert await _teardown_job_count(migrated_url) == 0
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_auditor", "all-projects")]

    asyncio.run(_run())


def test_admin_does_not_satisfy_operator(migrated_url: str) -> None:
    # ADR-0043 §2: platform_admin implies only platform_auditor, never platform_operator;
    # operator gating is its own axis, so an admin-only token is denied this operator tool.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile.reconcile_now(
                pool,
                _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN})),
                reaper=NullReaper(),
                upload_store=None,
            )
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"

    asyncio.run(_run())


def test_on_demand_pass_serializes_with_periodic_on_the_same_system_lock(
    migrated_url: str,
) -> None:
    """An on-demand pass blocks on the per-System advisory lock a periodic pass holds.

    Holding the per-System lock (the lock ``_repair_orphaned_systems`` takes) externally
    must stall the orphaned-System repair inside ``reconcile_now`` — proving the on-demand
    pass runs the same advisory-locked code path, not a second lock-free one. Released, the
    repair then proceeds and enqueues exactly one teardown.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with _pool(migrated_url) as pool, pool.connection() as holder:
            async with (
                holder.transaction(),
                advisory_xact_lock(holder, LockScope.SYSTEM, system_id),
            ):
                task = asyncio.create_task(
                    ops_reconcile.reconcile_now(
                        pool, _ctx(platform_roles=_OPERATOR), reaper=NullReaper(), upload_store=None
                    )
                )
                # Give the task time to reach and block on the per-System lock the holder owns.
                await asyncio.sleep(0.3)
                assert not task.done(), "reconcile_now did not block on the held System lock"
                assert await _teardown_job_count(migrated_url) == 0
            # holder transaction committed -> lock released; the repair now proceeds.
            resp = await task
        assert resp.status == "ok"
        assert resp.data["orphaned_systems"] == "1"
        assert await _teardown_job_count(migrated_url) == 1

    asyncio.run(_run())


def test_concurrent_on_demand_and_periodic_pass_enqueue_one_teardown(migrated_url: str) -> None:
    """A concurrent on-demand + periodic pass on one orphaned System enqueue one teardown.

    Both passes call the same ``reconcile_once``. The single-teardown outcome here is
    carried by the ``{system_id}:teardown`` dedup key; the advisory-lock *serialization*
    that prevents double-acting is proven separately by
    ``test_on_demand_pass_serializes_with_periodic_on_the_same_system_lock`` (a held lock
    actually stalls the on-demand repair). Together they show concurrent passes neither
    double-enqueue nor run the repair lock-free.
    """

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.RELEASED
            )
        async with _pool(migrated_url) as pool:
            on_demand = ops_reconcile.reconcile_now(
                pool, _ctx(platform_roles=_OPERATOR), reaper=NullReaper(), upload_store=None
            )
            periodic = reconcile_once(pool, NullReaper(), upload_store=None)
            results = await asyncio.gather(on_demand, periodic)
        assert results[0].status == "ok"
        assert await _teardown_job_count(migrated_url) == 1

    asyncio.run(_run())


@pytest.mark.usefixtures("migrated_url")
def test_register_resolves_upload_store_off_without_s3_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mirrors the periodic loop: no KDIVE_S3_* env -> the upload reaper stays off (None),
    # rather than raising, so the on-demand pass repairs the same set as the periodic one.
    monkeypatch.delenv("KDIVE_S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("KDIVE_S3_BUCKET", raising=False)
    assert ops_reconcile._resolve_upload_store() is None
