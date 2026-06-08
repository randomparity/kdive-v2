"""`inventory.list` auditor-read tool tests — cross-project fleet summary (#141).

The handler is called directly with an injected pool + RequestContext (the repo's unit
contract). Coverage maps to the #141 acceptance bullets:

* requires ``platform_auditor`` (satisfied by ``platform_admin``); a project-only token
  is denied; a denied ``platform_operator`` (holds a platform role) is audited; a denied
  project-only token is not.
* every served read writes exactly one ``platform_audit_log`` row and **no** ``audit_log``
  row.
* the summary spans every project's systems/allocations; filters narrow it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.models import Allocation, Resource, ResourceKind, System
from kdive.domain.state import AllocationState, ResourceStatus, SystemState
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.ops import inventory as inventory_tools
from kdive.security.authz.rbac import PlatformRole, Role

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_PROFILE = {"schema_version": 1, "arch": "x86_64"}


def _ctx(
    *,
    roles: dict[str, Role] | None = None,
    projects: tuple[str, ...] = (),
    platform_roles: frozenset[PlatformRole] = frozenset(),
    principal: str = "user-1",
) -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-1",
        projects=projects,
        roles=roles or {},
        platform_roles=platform_roles,
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
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


async def _alloc(
    conn: psycopg.AsyncConnection,
    resource_id: UUID,
    project: str,
    principal: str,
    state: AllocationState = AllocationState.ACTIVE,
) -> UUID:
    alloc = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal=principal,
            project=project,
            resource_id=resource_id,
            state=state,
        ),
    )
    return alloc.id


async def _system(
    conn: psycopg.AsyncConnection,
    allocation_id: UUID,
    project: str,
    principal: str,
    state: SystemState = SystemState.READY,
) -> UUID:
    sys = await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal=principal,
            project=project,
            allocation_id=allocation_id,
            state=state,
            provisioning_profile=_PROFILE,
        ),
    )
    return sys.id


async def _seed_two_projects(pool: AsyncConnectionPool) -> dict[str, UUID]:
    """proj-a: active alloc + ready system; proj-b: active alloc + crashed system."""
    ids: dict[str, UUID] = {}
    async with pool.connection() as conn:
        res = await _resource(conn)
        ids["res"] = res
        a = await _alloc(conn, res, "proj-a", "alice")
        ids["alloc_a"] = a
        ids["sys_a"] = await _system(conn, a, "proj-a", "alice", SystemState.READY)
        b = await _alloc(conn, res, "proj-b", "bob")
        ids["alloc_b"] = b
        ids["sys_b"] = await _system(conn, b, "proj-b", "bob", SystemState.CRASHED)
    return ids


async def _count_platform_audit(url: str) -> int:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM platform_audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _count_audit_log(url: str) -> int:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT principal, platform_role, tool, scope FROM platform_audit_log")
        return list(await cur.fetchall())


def _systems(resp: ToolResponse) -> list[dict[str, object]]:
    return json.loads(resp.data["systems"])


def _allocations(resp: ToolResponse) -> list[dict[str, object]]:
    return json.loads(resp.data["allocations"])


# ---- authorization ----------------------------------------------------------------


def test_auditor_lists_all_projects_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await inventory_tools.list_inventory(pool, ctx)
        assert resp.status == "ok"
        assert resp.error_category is None
        sys_by_project = {s["project"]: s for s in _systems(resp)}
        assert set(sys_by_project) == {"proj-a", "proj-b"}
        assert sys_by_project["proj-a"]["state"] == "ready"
        assert sys_by_project["proj-b"]["state"] == "crashed"
        alloc_projects = {a["project"] for a in _allocations(resp)}
        assert alloc_projects == {"proj-a", "proj-b"}
        assert resp.data["truncated"] == "false"
        # Exactly one platform_audit_log row, zero audit_log writes.
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("user-1", "platform_auditor", "inventory.list", "all-projects")]
        assert await _count_audit_log(migrated_url) == 0

    asyncio.run(_run())


def test_admin_satisfies_auditor_gate(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}))
            resp = await inventory_tools.list_inventory(pool, ctx)
        assert resp.status == "ok"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_admin"

    asyncio.run(_run())


def test_project_only_token_denied_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await inventory_tools.list_inventory(pool, ctx)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert resp.suggested_next_actions == ["inventory.list"]
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_operator_denied_but_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))
            resp = await inventory_tools.list_inventory(pool, ctx)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_operator"
        assert rows[0][3] == "all-projects"

    asyncio.run(_run())


# ---- filters ----------------------------------------------------------------------


def test_filter_by_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await inventory_tools.list_inventory(pool, ctx, project="proj-a")
        assert resp.status == "ok"
        assert {s["project"] for s in _systems(resp)} == {"proj-a"}
        assert {a["project"] for a in _allocations(resp)} == {"proj-a"}

    asyncio.run(_run())


def test_filter_by_resource(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ids = await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await inventory_tools.list_inventory(pool, ctx, resource_id=str(ids["res"]))
        assert resp.status == "ok"
        assert {a["resource_id"] for a in _allocations(resp)} == {str(ids["res"])}

    asyncio.run(_run())


def test_filter_by_unknown_project_is_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await inventory_tools.list_inventory(pool, ctx, project="proj-z")
        assert resp.status == "ok"
        assert _systems(resp) == []
        assert _allocations(resp) == []
        # An empty result still counts as an oversight read and is audited.
        assert await _count_platform_audit(migrated_url) == 1

    asyncio.run(_run())


def test_malformed_resource_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await inventory_tools.list_inventory(pool, ctx, resource_id="not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.suggested_next_actions == ["inventory.list"]

    asyncio.run(_run())
