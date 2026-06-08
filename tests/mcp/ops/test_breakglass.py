"""Break-glass ops.force_teardown / ops.force_release tool tests (#140, ADR-0062 §4).

Handlers are driven directly with an injected pool + RequestContext (the repo unit
contract). Coverage maps to the #140 acceptance bullets:

* force_release / force_teardown succeed against a cross-project object for a
  platform_admin who is NOT a member of the object's project, fully bypassing the
  three-check destructive gate;
* the audit write succeeds despite non-membership (guard-exempt record_system, not the
  membership-guarded record), with pinned per-allocation audit_log row counts;
* a blank reason is rejected; a platform_operator (non-admin) is denied;
* every authorized call writes one platform_audit_log row, written before — and committed
  independently of — the release/teardown mechanic, so a failed/stale/idempotent path is
  still audited.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.models import (
    Allocation,
    Resource,
    ResourceKind,
    System,
)
from kdive.domain.state import AllocationState, ResourceStatus, SystemState
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.ops import breakglass
from kdive.security.rbac import AuthorizationError, PlatformRole

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_TARGET_PROJECT = "tenant-x"
_PROFILE = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/fedora-40.qcow2"},
            "crashkernel": "256M",
        }
    },
}


def _admin_ctx(*, principal: str = "ops-admin") -> RequestContext:
    """A platform_admin holding NO project membership — the break-glass principal."""
    return RequestContext(
        principal=principal,
        agent_session="sess-admin",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
    )


def _operator_ctx() -> RequestContext:
    return RequestContext(
        principal="ops-operator",
        agent_session="sess-operator",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _resource(conn: psycopg.AsyncConnection) -> UUID:
    res = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={},
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )
    return res.id


async def _budget(conn: psycopg.AsyncConnection, project: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO budgets (project, limit_kcu, spent_kcu) VALUES (%s, 1000, 0)",
            (project,),
        )


async def _alloc(
    pool: AsyncConnectionPool,
    *,
    state: AllocationState,
    project: str = _TARGET_PROJECT,
    sized: bool = True,
    with_budget: bool = True,
) -> UUID:
    async with pool.connection() as conn:
        resource_id = await _resource(conn)
        if with_budget:
            await _budget(conn, project)
        active_started = _DT if state is AllocationState.ACTIVE else None
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="tenant-user",
                project=project,
                resource_id=resource_id,
                state=state,
                requested_vcpus=2 if sized else None,
                requested_memory_gb=4 if sized else None,
                active_started_at=active_started,
            ),
        )
    return alloc.id


async def _system(pool: AsyncConnectionPool, *, state: SystemState) -> UUID:
    alloc = await _alloc(pool, state=AllocationState.ACTIVE)
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="tenant-user",
                project=_TARGET_PROJECT,
                allocation_id=alloc,
                state=state,
                provisioning_profile=_PROFILE,
            ),
        )
    return system.id


async def _alloc_state(url: str, alloc_id: UUID) -> str:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT principal, platform_role, tool, scope FROM platform_audit_log")
        return list(await cur.fetchall())


async def _count_platform_audit(url: str) -> int:
    return len(await _platform_audit_rows(url))


async def _audit_log_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, agent_session, project, transition FROM audit_log ORDER BY ts, id"
        )
        return list(await cur.fetchall())


async def _job_count(url: str, dedup_key: str) -> int:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM jobs WHERE dedup_key = %s", (dedup_key,))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


# ---- force_release -----------------------------------------------------------------


def test_force_release_cross_project_active_success(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _alloc(pool, state=AllocationState.ACTIVE)
            resp = await breakglass.force_release(
                pool, _admin_ctx(), allocation_id=str(alloc_id), reason="stuck tenant lease"
            )
        assert resp.status == "released"
        assert resp.error_category is None
        assert await _alloc_state(migrated_url, alloc_id) == "released"
        assert await _count_platform_audit(migrated_url) == 1

    asyncio.run(_run())


def test_force_release_active_writes_two_guard_exempt_audit_rows(migrated_url: str) -> None:
    # The reused release path writes two audit_log transition rows (active->releasing,
    # releasing->released) via the guard-exempt record_system writer (agent_session NULL)
    # under the admin principal against the TARGET project — record() would have raised on
    # non-membership.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _alloc(pool, state=AllocationState.ACTIVE)
            await breakglass.force_release(
                pool, _admin_ctx(), allocation_id=str(alloc_id), reason="evict"
            )
        rows = await _audit_log_rows(migrated_url)
        # Two transitions written; ts/id give no stable order within one txn, so assert the set.
        assert {r[3] for r in rows} == {"active->releasing", "releasing->released"}
        assert len(rows) == 2
        for principal, agent_session, project, _ in rows:
            assert principal == "ops-admin"
            assert agent_session is None  # record_system passes no agent_session
            assert project == _TARGET_PROJECT

    asyncio.run(_run())


def test_force_release_granted_writes_two_audit_rows(migrated_url: str) -> None:
    # GRANTED is in _RELEASABLE, so it too takes the first transition: granted->releasing,
    # then releasing->released — two guard-exempt rows.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _alloc(pool, state=AllocationState.GRANTED)
            await breakglass.force_release(
                pool, _admin_ctx(), allocation_id=str(alloc_id), reason="evict"
            )
        rows = await _audit_log_rows(migrated_url)
        assert {r[3] for r in rows} == {"granted->releasing", "releasing->released"}
        assert len(rows) == 2

    asyncio.run(_run())


def test_force_release_from_releasing_writes_one_audit_row(migrated_url: str) -> None:
    # An allocation already mid-release (RELEASING) only takes the second transition:
    # releasing->released — exactly one guard-exempt row.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _alloc(pool, state=AllocationState.RELEASING)
            resp = await breakglass.force_release(
                pool, _admin_ctx(), allocation_id=str(alloc_id), reason="finish"
            )
        assert resp.status == "released"
        rows = await _audit_log_rows(migrated_url)
        assert [r[3] for r in rows] == ["releasing->released"]

    asyncio.run(_run())


def test_force_release_blank_reason_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _alloc(pool, state=AllocationState.ACTIVE)
            resp = await breakglass.force_release(
                pool, _admin_ctx(), allocation_id=str(alloc_id), reason="   "
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert await _alloc_state(migrated_url, alloc_id) == "active"
        assert await _count_platform_audit(migrated_url) == 0
        assert await _audit_log_rows(migrated_url) == []

    asyncio.run(_run())


def test_force_release_operator_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _alloc(pool, state=AllocationState.ACTIVE)
            try:
                await breakglass.force_release(
                    pool, _operator_ctx(), allocation_id=str(alloc_id), reason="nope"
                )
                raise AssertionError("expected AuthorizationError for a platform_operator")
            except AuthorizationError:
                pass
            assert await _alloc_state(migrated_url, alloc_id) == "active"
            assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_force_release_no_persisted_size_typed_failure_but_audited(migrated_url: str) -> None:
    # An active allocation with NULL size + a budget row makes reconcile raise
    # CONFIGURATION_ERROR. Break-glass must return a typed failure (not leak the raw
    # exception), and the platform_audit_log row is still written (audit-before-release).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _alloc(
                pool, state=AllocationState.ACTIVE, sized=False, with_budget=True
            )
            resp = await breakglass.force_release(
                pool, _admin_ctx(), allocation_id=str(alloc_id), reason="unstick"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert await _count_platform_audit(migrated_url) == 1

    asyncio.run(_run())


def test_force_release_terminal_allocation_stale_but_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _alloc(pool, state=AllocationState.RELEASED)
            resp = await breakglass.force_release(
                pool, _admin_ctx(), allocation_id=str(alloc_id), reason="retry"
            )
        assert resp.status == "error"
        assert resp.error_category == "stale_handle"
        assert await _count_platform_audit(migrated_url) == 1

    asyncio.run(_run())


def test_force_release_bad_uuid_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await breakglass.force_release(
                pool, _admin_ctx(), allocation_id="not-a-uuid", reason="x"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_force_release_missing_allocation_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await breakglass.force_release(
                pool, _admin_ctx(), allocation_id=str(uuid4()), reason="x"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


# ---- force_teardown ----------------------------------------------------------------


def test_force_teardown_cross_project_success(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _system(pool, state=SystemState.READY)
            resp = await breakglass.force_teardown(
                pool, _admin_ctx(), system_id=str(sys_id), reason="abandoned host"
            )
        assert resp.error_category is None
        assert await _job_count(migrated_url, f"{sys_id}:teardown") == 1
        assert await _count_platform_audit(migrated_url) == 1

    asyncio.run(_run())


def test_force_teardown_blank_reason_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _system(pool, state=SystemState.READY)
            resp = await breakglass.force_teardown(
                pool, _admin_ctx(), system_id=str(sys_id), reason=""
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert await _job_count(migrated_url, f"{sys_id}:teardown") == 0
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_force_teardown_operator_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _system(pool, state=SystemState.READY)
            try:
                await breakglass.force_teardown(
                    pool, _operator_ctx(), system_id=str(sys_id), reason="nope"
                )
                raise AssertionError("expected AuthorizationError for a platform_operator")
            except AuthorizationError:
                pass
            assert await _job_count(migrated_url, f"{sys_id}:teardown") == 0
            assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_force_teardown_idempotent_on_torn_down(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _system(pool, state=SystemState.TORN_DOWN)
            resp = await breakglass.force_teardown(
                pool, _admin_ctx(), system_id=str(sys_id), reason="already gone"
            )
        assert resp.status == "torn_down"
        assert await _job_count(migrated_url, f"{sys_id}:teardown") == 0
        assert await _count_platform_audit(migrated_url) == 1

    asyncio.run(_run())


def test_force_teardown_twice_dedups_to_one_job(migrated_url: str) -> None:
    # Re-invoking break-glass teardown enqueues no second job (the {uid}:teardown dedup key),
    # and the read+check+enqueue runs under the SYSTEM lock so the second call is race-safe.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _system(pool, state=SystemState.READY)
            first = await breakglass.force_teardown(
                pool, _admin_ctx(), system_id=str(sys_id), reason="evict"
            )
            second = await breakglass.force_teardown(
                pool, _admin_ctx(), system_id=str(sys_id), reason="evict again"
            )
        assert first.object_id == second.object_id  # same dedup'd job
        assert await _job_count(migrated_url, f"{sys_id}:teardown") == 1
        # Both attempts are audited (the accountability row records each break-glass call).
        assert await _count_platform_audit(migrated_url) == 2

    asyncio.run(_run())


def test_force_teardown_bad_uuid_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await breakglass.force_teardown(
                pool, _admin_ctx(), system_id="not-a-uuid", reason="x"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_force_teardown_missing_system_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await breakglass.force_teardown(
                pool, _admin_ctx(), system_id=str(uuid4()), reason="x"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())
