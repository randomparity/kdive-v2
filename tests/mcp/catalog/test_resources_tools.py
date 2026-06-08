"""resources.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation, System
from kdive.domain.state import AllocationState, SystemState
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.catalog import resources as resources_tools
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.security.authz.rbac import PlatformRole
from kdive.services.allocation_release import ReleaseOutcome
from kdive.services.resource_discovery import register_discovered_resource
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

CTX = RequestContext(principal="user-1", agent_session="s", projects=("proj",))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _discovery(cap: int = 2) -> LocalLibvirtDiscovery:
    return LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=cap,
    )


async def _register(pool: AsyncConnectionPool) -> str:
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn, _discovery().list_resources()[0], pool="local-libvirt", cost_class="local"
        )
    return str(res.id)


def test_list_returns_host_with_flat_capability_projection(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            responses = await resources_tools.list_resources_tool(pool, CTX, kind=None)
        assert responses.object_id == "resources"
        assert responses.status == "ok"
        items = responses.items
        assert len(items) == 1
        resp = items[0]
        assert resp.object_id == res_id
        assert resp.status == "available"
        assert resp.data["kind"] == "local-libvirt"
        assert resp.data["arch"] == "x86_64"
        assert resp.data["vcpus"] == "8"
        assert resp.data["memory_mb"] == "16384"
        assert resp.data["transports"] == "gdbstub"
        assert resp.data["concurrent_allocation_cap"] == "2"

    asyncio.run(_run())


def test_list_kind_filter_miss_is_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool)
            responses = await resources_tools.list_resources_tool(pool, CTX, kind="nope")
        assert responses.status == "error"
        assert responses.error_category == "configuration_error"

    asyncio.run(_run())


def test_list_malformed_resource_row_degrades_to_infrastructure_failure(
    migrated_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            async with pool.connection() as conn:
                await conn.execute("UPDATE resources SET capabilities = '[]'::jsonb")
            caplog.set_level(logging.WARNING, logger=resources_tools.__name__)
            responses = await resources_tools.list_resources_tool(pool, CTX, kind="local-libvirt")
        items = responses.items
        assert len(items) == 1
        assert items[0].object_id == res_id
        assert items[0].status == "error"
        assert items[0].error_category == "infrastructure_failure"
        assert any(
            record.exc_info is not None and f"resource {res_id}" in record.message
            for record in caplog.records
        )

    asyncio.run(_run())


def test_describe_adds_pool_cost_host(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.describe_resource(pool, CTX, res_id)
        assert resp.status == "available"
        assert resp.data["pool"] == "local-libvirt"
        assert resp.data["cost_class"] == "local"
        assert resp.data["host_uri"] == "qemu:///system"

    asyncio.run(_run())


def test_describe_unknown_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.describe_resource(pool, CTX, str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_describe_malformed_id_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.describe_resource(pool, CTX, "not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


_OPERATOR = RequestContext(
    principal="op-1",
    agent_session="s",
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
)
_NON_OPERATOR = RequestContext(principal="user-1", agent_session="s", projects=("proj",))
_AUDITOR = RequestContext(
    principal="auditor-1",
    agent_session="s",
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}),
)


async def _row(pool: AsyncConnectionPool, res_id: str) -> dict[str, Any]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT status, cordoned FROM resources WHERE id = %s", (UUID(res_id),))
        fetched = await cur.fetchone()
    assert fetched is not None
    status, cordoned = fetched
    return {"status": status, "cordoned": cordoned}


async def _platform_audit_count(pool: AsyncConnectionPool, tool: str) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM platform_audit_log WHERE tool = %s", (tool,))
        fetched = await cur.fetchone()
    assert fetched is not None
    return int(fetched[0])


async def _platform_audit_rows(pool: AsyncConnectionPool) -> list[tuple[object, ...]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, platform_role, tool, scope FROM platform_audit_log ORDER BY ts"
        )
        return list(await cur.fetchall())


def test_set_status_changes_health_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="degraded"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_count(pool, "resources.set_status")
        assert resp.status == "degraded"
        assert row == {"status": "degraded", "cordoned": False}
        assert audited == 1

    asyncio.run(_run())


def test_set_status_same_value_is_noop_success(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="available"
            )
            row = await _row(pool, res_id)
        assert resp.status == "available"
        assert row["status"] == "available"

    asyncio.run(_run())


def test_set_status_invalid_value_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="nope"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_set_status_unknown_host_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=str(uuid4()), status="offline"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_set_status_does_not_clear_cordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await resources_tools.cordon_resource(pool, _OPERATOR, resource_id=res_id)
            await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="offline"
            )
            row = await _row(pool, res_id)
        # set_status offline must not clear an operator's cordon (orthogonal axes).
        assert row == {"status": "offline", "cordoned": True}

    asyncio.run(_run())


def test_cordon_then_uncordon_toggles_only_cordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            # Make the host degraded first; cordon/uncordon must not touch status.
            await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="degraded"
            )
            cordoned = await resources_tools.cordon_resource(pool, _OPERATOR, resource_id=res_id)
            after_cordon = await _row(pool, res_id)
            await resources_tools.uncordon_resource(pool, _OPERATOR, resource_id=res_id)
            after_uncordon = await _row(pool, res_id)
            cordon_audited = await _platform_audit_count(pool, "resources.cordon")
            uncordon_audited = await _platform_audit_count(pool, "resources.uncordon")
        assert cordoned.status == "degraded"
        assert after_cordon == {"status": "degraded", "cordoned": True}
        # uncordon does not change status: still degraded.
        assert after_uncordon == {"status": "degraded", "cordoned": False}
        assert cordon_audited == 1
        assert uncordon_audited == 1

    asyncio.run(_run())


def test_cordon_unknown_host_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.cordon_resource(pool, _OPERATOR, resource_id=str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_set_status_denied_for_non_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _NON_OPERATOR, resource_id=res_id, status="offline"
            )
            row = await _row(pool, res_id)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        # The denied call must not have mutated the host.
        assert row == {"status": "available", "cordoned": False}

    asyncio.run(_run())


def test_set_status_denied_for_auditor_is_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _AUDITOR, resource_id=res_id, status="offline"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert row == {"status": "available", "cordoned": False}
        assert audited == [
            ("auditor-1", "platform_auditor", "resources.set_status", f"resource:{res_id}")
        ]

    asyncio.run(_run())


def test_cordon_denied_for_non_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.cordon_resource(pool, _NON_OPERATOR, resource_id=res_id)
            row = await _row(pool, res_id)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert row["cordoned"] is False

    asyncio.run(_run())


# ---- resources.drain: release classifier (pure unit, no DB) -------------------------
# `skipped` (STALE_HANDLE) is a race-only outcome unreachable by DB seeding, so the
# released/skipped/failed mapping is pinned here against ReleaseOutcome directly (#143).


def test_classify_released_is_released_status() -> None:
    item = resources_tools._classify_drain_release("a-1", ReleaseOutcome(released=True))
    assert item.object_id == "a-1"
    assert item.status == "released"
    assert item.error_category is None
    assert "current_status" not in item.data


def test_classify_stale_handle_is_skipped_with_status() -> None:
    item = resources_tools._classify_drain_release(
        "a-2",
        ReleaseOutcome(
            released=False, category=ErrorCategory.STALE_HANDLE, current_status="released"
        ),
    )
    assert item.status == "skipped"
    assert item.error_category is None
    assert item.data["current_status"] == "released"


def test_classify_failed_with_status_carries_current_status() -> None:
    item = resources_tools._classify_drain_release(
        "a-3",
        ReleaseOutcome(
            released=False, category=ErrorCategory.CONFIGURATION_ERROR, current_status="active"
        ),
    )
    assert item.status == "error"
    assert item.error_category == "configuration_error"
    assert item.data["current_status"] == "active"


def test_classify_failed_without_status_omits_current_status() -> None:
    # The reconcile-failure path returns no current_status; the key must be omitted, not null
    # (matching the sibling break-glass envelope, breakglass.py:167).
    item = resources_tools._classify_drain_release(
        "a-4",
        ReleaseOutcome(released=False, category=ErrorCategory.CONFIGURATION_ERROR),
    )
    assert item.status == "error"
    assert item.error_category == "configuration_error"
    assert "current_status" not in item.data


# ---- resources.drain: handler (DB-backed) ------------------------------------------

_ADMIN = RequestContext(
    principal="admin-1",
    agent_session="s",
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
)

_DRAIN_DT = datetime(2026, 1, 1, tzinfo=UTC)
_DRAIN_PROJECT = "tenant-x"
_DRAIN_PROFILE = {
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


async def _ensure_budget(conn: AsyncConnection, project: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO budgets (project, limit_kcu, spent_kcu) VALUES (%s, 1000, 0) "
            "ON CONFLICT (project) DO NOTHING",
            (project,),
        )


async def _alloc_on(
    pool: AsyncConnectionPool,
    res_id: str,
    *,
    state: AllocationState,
    project: str = _DRAIN_PROJECT,
    sized: bool = True,
) -> UUID:
    async with pool.connection() as conn:
        await _ensure_budget(conn, project)
        active_started = _DRAIN_DT if state is AllocationState.ACTIVE else None
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DRAIN_DT,
                updated_at=_DRAIN_DT,
                principal="tenant-user",
                project=project,
                resource_id=UUID(res_id),
                state=state,
                requested_vcpus=2 if sized else None,
                requested_memory_gb=4 if sized else None,
                active_started_at=active_started,
            ),
        )
    return alloc.id


async def _system_on(pool: AsyncConnectionPool, alloc_id: UUID, *, state: SystemState) -> UUID:
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DRAIN_DT,
                updated_at=_DRAIN_DT,
                principal="tenant-user",
                project=_DRAIN_PROJECT,
                allocation_id=alloc_id,
                state=state,
                provisioning_profile=_DRAIN_PROFILE,
            ),
        )
    return system.id


async def _alloc_state(pool: AsyncConnectionPool, alloc_id: UUID) -> str | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
        row = await cur.fetchone()
    return None if row is None else str(row[0])


async def _system_state(pool: AsyncConnectionPool, system_id: UUID) -> str | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    return None if row is None else str(row[0])


async def _audit_log_count(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def _statuses(resp: Any) -> list[str]:
    return [item.status for item in resp.items]


# -- passive --------------------------------------------------------------------------


def test_drain_passive_cordons_and_reports_live_allocations(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            active = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            granted = await _alloc_on(pool, res_id, state=AllocationState.GRANTED)
            await _alloc_on(pool, res_id, state=AllocationState.RELEASED)  # terminal: excluded
            resp = await resources_tools.drain_resource(
                pool, _OPERATOR, resource_id=res_id, mode="passive"
            )
            row = await _row(pool, res_id)
            breakglass_rows = await _audit_log_count(pool)
            active_state = await _alloc_state(pool, active)
            granted_state = await _alloc_state(pool, granted)
        assert resp.object_id == res_id
        assert resp.status == "cordoned"
        assert row == {"status": "available", "cordoned": True}
        assert {item.object_id for item in resp.items} == {str(active), str(granted)}
        assert sorted(_statuses(resp)) == ["active", "granted"]
        # Passive leaves them running: no release transitions written.
        assert breakglass_rows == 0
        assert active_state == "active"
        assert granted_state == "granted"

    asyncio.run(_run())


def test_drain_passive_empty_host_cordons_zero_items(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _OPERATOR, resource_id=res_id, mode="passive"
            )
            row = await _row(pool, res_id)
        assert resp.status == "cordoned"
        assert resp.items == []
        assert row["cordoned"] is True

    asyncio.run(_run())


def test_drain_passive_denied_for_non_operator_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _NON_OPERATOR, resource_id=res_id, mode="passive"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert row["cordoned"] is False  # denied call did not cordon
        assert audited == []  # project-only denial is not recorded

    asyncio.run(_run())


def test_drain_passive_denied_for_auditor_is_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _AUDITOR, resource_id=res_id, mode="passive"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
        assert resp.error_category == "authorization_denied"
        assert row["cordoned"] is False
        assert audited == [
            ("auditor-1", "platform_auditor", "resources.drain", f"resource:{res_id}")
        ]

    asyncio.run(_run())


def test_drain_passive_denied_for_admin_only_token(migrated_url: str) -> None:
    # The role model is non-hierarchical (admin implies only auditor), so an admin-only token
    # is denied passive drain, which is a platform_operator action (ADR-0062 §3, rbac.py).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="passive"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
        assert resp.error_category == "authorization_denied"
        assert row["cordoned"] is False
        assert audited == [("admin-1", "platform_admin", "resources.drain", f"resource:{res_id}")]

    asyncio.run(_run())


# -- force_release --------------------------------------------------------------------


def test_drain_force_release_operator_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            alloc = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            resp = await resources_tools.drain_resource(
                pool, _OPERATOR, resource_id=res_id, mode="force_release", reason="evict"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
            alloc_state = await _alloc_state(pool, alloc)
        assert resp.error_category == "authorization_denied"
        assert row["cordoned"] is False  # denied before cordon
        assert alloc_state == "active"  # untouched
        assert audited == [("op-1", "platform_operator", "resources.drain", f"resource:{res_id}")]

    asyncio.run(_run())


def test_drain_force_release_blank_reason_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            alloc = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="   "
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
            alloc_state = await _alloc_state(pool, alloc)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert row["cordoned"] is False  # blank reason rejected before cordon
        assert alloc_state == "active"
        assert audited == []

    asyncio.run(_run())


def test_drain_force_release_admin_empties_host(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            a1 = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            a2 = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="maintenance"
            )
            row = await _row(pool, res_id)
            drain_rows = await _platform_audit_count(pool, "resources.drain")
            transitions = await _audit_log_count(pool)
            a1_state = await _alloc_state(pool, a1)
            a2_state = await _alloc_state(pool, a2)
        assert resp.status == "cordoned"
        assert row["cordoned"] is True
        assert _statuses(resp) == ["released", "released"]
        assert resp.data["released"] == "2"
        assert a1_state == "released"
        assert a2_state == "released"
        # 1 cordon row + 1 break-glass row per allocation.
        assert drain_rows == 3
        # 2 guard-exempt transition rows per released allocation.
        assert transitions == 4

    asyncio.run(_run())


def test_drain_force_release_empties_every_tenant_on_the_host(migrated_url: str) -> None:
    # The escalation to platform_admin exists because force_release empties EVERY tenant's
    # allocations on the host (ADR-0062 §3) — verify the snapshot is not project-scoped and
    # each release is attributed to its own project.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            a_x = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE, project="tenant-x")
            a_y = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE, project="tenant-y")
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="decommission"
            )
            x_state = await _alloc_state(pool, a_x)
            y_state = await _alloc_state(pool, a_y)
            audited = await _platform_audit_rows(pool)
        assert _statuses(resp) == ["released", "released"]
        assert resp.data["released"] == "2"
        assert x_state == "released"
        assert y_state == "released"
        # Each cross-tenant release is attributed to its own project via the break-glass scope.
        breakglass_scopes = {scope for _, _, tool, scope in audited if tool == "resources.drain"}
        assert f"tenant-x:{a_x}" in breakglass_scopes
        assert f"tenant-y:{a_y}" in breakglass_scopes

    asyncio.run(_run())


def test_drain_force_release_empty_host_is_idempotent_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="already empty"
            )
            row = await _row(pool, res_id)
            drain_rows = await _platform_audit_count(pool, "resources.drain")
            transitions = await _audit_log_count(pool)
        assert resp.status == "cordoned"
        assert resp.items == []
        assert resp.data["released"] == "0"
        assert row["cordoned"] is True
        assert drain_rows == 1  # only the cordon row
        assert transitions == 0  # no break-glass releases

    asyncio.run(_run())


def test_drain_force_release_partial_failure_observable_and_reinvokable(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            ok = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            # NULL size + a budget row makes reconcile raise CONFIGURATION_ERROR.
            bad = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE, sized=False)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="drain"
            )
            released = [i for i in resp.items if i.status == "released"]
            failed = [i for i in resp.items if i.status == "error"]
            row = await _row(pool, res_id)
            ok_state = await _alloc_state(pool, ok)
            bad_state = await _alloc_state(pool, bad)

            # Re-invoke: the released one is gone from the snapshot; the failed one returns again.
            resp2 = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="drain again"
            )
            bad_state_after = await _alloc_state(pool, bad)
        assert len(released) == 1 and len(failed) == 1
        assert failed[0].error_category == "configuration_error"
        assert "current_status" not in failed[0].data  # reconcile path carries none
        assert ok_state == "released"
        # The failed one rolled back to active (not stranded in releasing) — re-releasable.
        assert bad_state == "active"
        assert row["cordoned"] is True
        assert _statuses(resp2) == ["error"]
        assert resp2.items[0].object_id == str(bad)
        assert bad_state_after == "active"

    asyncio.run(_run())


def test_drain_force_release_leaves_system_untouched(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            alloc = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            system = await _system_on(pool, alloc, state=SystemState.READY)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="evict"
            )
            alloc_state = await _alloc_state(pool, alloc)
            system_state = await _system_state(pool, system)
        assert _statuses(resp) == ["released"]
        assert alloc_state == "released"
        assert system_state == "ready"  # drain does not tear down Systems

    asyncio.run(_run())


# -- input validation -----------------------------------------------------------------


def test_drain_bad_uuid_is_error_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.drain_resource(
                pool, _OPERATOR, resource_id="not-a-uuid", mode="passive"
            )
            audited = await _platform_audit_rows(pool)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert audited == []

    asyncio.run(_run())


def test_drain_unknown_host_is_error_uncordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.drain_resource(
                pool, _OPERATOR, resource_id=str(uuid4()), mode="passive"
            )
            audited = await _platform_audit_rows(pool)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert audited == []  # no host to cordon, nothing audited

    asyncio.run(_run())


def test_drain_unknown_mode_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="migrate", reason="x"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert row["cordoned"] is False  # unknown mode → no role resolved → no cordon
        assert audited == []

    asyncio.run(_run())
