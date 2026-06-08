"""Accounting report tool tests — two explicit report forms + read-shape audit.

The handler is called directly with an injected pool + RequestContext (the repo's unit
contract). Coverage maps to the #97 acceptance bullets:

* all-projects: platform_auditor / platform_admin rollup ≥2 projects, always audited;
  SoD denials (project-only token; platform_operator) — denial audited iff the caller
  holds ≥1 platform role.
* granted-set: default to ctx.projects (role-less membership dropped), named non-member
  rejected, zero-project empty rollup, audit-iff-shape (>1 project OR group_by=principal),
  never a per-project audit_log row.
* group_by=principal over a window, in both scope forms.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.accounting.reports import report_all_projects, report_granted_set
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, Role

_DT = datetime(2026, 1, 1, tzinfo=UTC)


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
    conn: psycopg.AsyncConnection, resource_id: UUID, project: str, principal: str
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
            state=AllocationState.ACTIVE,
            requested_vcpus=2,
            requested_memory_gb=4,
        ),
    )
    return alloc.id


async def _ledger(
    conn: psycopg.AsyncConnection,
    project: str,
    alloc_id: UUID,
    event_type: str,
    kcu: str,
    ts: datetime = _DT,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ledger (id, ts, project, allocation_id, cost_class, "
            "event_type, kcu_delta) VALUES (%s, %s, %s, %s, 'local', %s, %s)",
            (uuid4(), ts, project, alloc_id, event_type, Decimal(kcu)),
        )


async def _budget(conn: psycopg.AsyncConnection, project: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO budgets (project, limit_kcu, spent_kcu) VALUES (%s, 1000, 0)",
            (project,),
        )


async def _seed_two_projects(pool: AsyncConnectionPool) -> None:
    """proj-a (alice: +10/-3) and proj-b (bob: +20/+5), both with budget rows."""
    async with pool.connection() as conn:
        res = await _resource(conn)
        await _budget(conn, "proj-a")
        await _budget(conn, "proj-b")
        a = await _alloc(conn, res, "proj-a", "alice")
        b = await _alloc(conn, res, "proj-b", "bob")
        await _ledger(conn, "proj-a", a, "reserved", "10")
        await _ledger(conn, "proj-a", a, "reconciled", "-3")
        await _ledger(conn, "proj-b", b, "reserved", "20")
        await _ledger(conn, "proj-b", b, "reconciled", "5")


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


def _rows(resp: ToolResponse) -> list[dict[str, object]]:
    return json.loads(resp.data["rows"])


def _total(resp: ToolResponse) -> dict[str, object]:
    return json.loads(resp.data["total"])


# ---- all-projects form ------------------------------------------------------------


def test_all_projects_auditor_rollup_and_audit_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await report_all_projects(pool, ctx)
        assert resp.status == "ok"
        assert resp.error_category is None
        by_project = {r["project"]: r for r in _rows(resp)}
        assert by_project["proj-a"]["variance"] == "-13.0000"
        assert by_project["proj-b"]["variance"] == "-15.0000"
        total = _total(resp)
        assert total["reserved"] == "30.0000"
        assert total["reconciled"] == "2.0000"
        assert total["variance"] == "-28.0000"
        # Exactly one platform_audit_log row (role recorded), zero per-project audit_log.
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [
            ("user-1", "platform_auditor", "accounting.report_all_projects", "all-projects")
        ]
        assert await _count_audit_log(migrated_url) == 0

    asyncio.run(_run())


def test_all_projects_admin_satisfies_auditor_gate(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}))
            resp = await report_all_projects(pool, ctx)
        assert resp.status == "ok"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_admin"

    asyncio.run(_run())


def test_all_projects_project_only_token_denied_unaudited(migrated_url: str) -> None:
    # SoD: a project-scoped admin holds no platform role → denied, and the denial is NOT
    # audited (routine non-grant on an openly-callable read; no write amplification).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await report_all_projects(pool, ctx)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert resp.suggested_next_actions == ["accounting.report_all_projects"]
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_all_projects_operator_denied_but_audited(migrated_url: str) -> None:
    # SoD: platform_operator does NOT satisfy the auditor gate, but holds a platform role,
    # so the over-reach denial IS audited (the accountability target).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))
            resp = await report_all_projects(pool, ctx)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert resp.suggested_next_actions == ["accounting.report_all_projects"]
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_operator"
        assert rows[0][3] == "all-projects"

    asyncio.run(_run())


# ---- granted-set form -------------------------------------------------------------


def test_granted_set_default_resolves_member_projects_with_role(migrated_url: str) -> None:
    # viewer on A+B, bare member of C → rollup over exactly A+B (C dropped); one audit row
    # (>1 project, platform_role null); no per-project audit_log row.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(
                roles={"proj-a": Role.VIEWER, "proj-b": Role.VIEWER},
                projects=("proj-a", "proj-b", "proj-c"),
            )
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert {r["project"] for r in _rows(resp)} == {"proj-a", "proj-b"}
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] is None  # platform_role null for a member read
        assert rows[0][3] == "granted-set:proj-a,proj-b"
        assert await _count_audit_log(migrated_url) == 0

    asyncio.run(_run())


def test_granted_set_audits_two_projects_even_when_only_one_has_spend(migrated_url: str) -> None:
    # The audit trigger counts the authorized set (A+B), not the returned rows: only A has
    # ledger rows but the 2-project read is still audited.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                await _budget(conn, "proj-a")
                await _budget(conn, "proj-b")
                a = await _alloc(conn, res, "proj-a", "alice")
                await _ledger(conn, "proj-a", a, "reserved", "5")
            ctx = _ctx(
                roles={"proj-a": Role.VIEWER, "proj-b": Role.VIEWER},
                projects=("proj-a", "proj-b"),
            )
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert {r["project"] for r in _rows(resp)} == {"proj-a"}
        assert await _count_platform_audit(migrated_url) == 1

    asyncio.run(_run())


def test_granted_set_all_roleless_memberships_empty_rollup(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(roles={}, projects=("proj-a", "proj-b"))
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert _rows(resp) == []
        assert _total(resp)["reserved"] == "0.0000"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_granted_set_named_non_member_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            try:
                await report_granted_set(pool, ctx, projects=["proj-a", "proj-z"])
                raise AssertionError("expected AuthorizationError for a named non-member")
            except AuthorizationError:
                pass
            assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_granted_set_named_roleless_project_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            # proj-c is a bare membership (no role): naming it explicitly must raise.
            ctx = _ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a", "proj-c"))
            try:
                await report_granted_set(pool, ctx, projects=["proj-c"])
                raise AssertionError("expected AuthorizationError for a named role-less project")
            except AuthorizationError:
                pass

    asyncio.run(_run())


def test_granted_set_single_project_ungrouped_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert {r["project"] for r in _rows(resp)} == {"proj-a"}
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_granted_set_single_project_group_by_principal_audited(migrated_url: str) -> None:
    # group_by=principal is the load-bearing audit trigger even for one project.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            resp = await report_granted_set(pool, ctx, group_by="principal")
        assert resp.status == "ok"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] is None

    asyncio.run(_run())


def test_granted_set_zero_resolution_empty_rollup(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(roles={}, projects=())
            resp = await report_granted_set(pool, ctx)
        assert resp.status == "ok"
        assert _rows(resp) == []
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


# ---- group_by=principal over a window, both scope forms --------------------------


def test_group_by_principal_window_totals_granted_set(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                await _budget(conn, "proj-a")
                alice = await _alloc(conn, res, "proj-a", "alice")
                bob = await _alloc(conn, res, "proj-a", "bob")
                inside = datetime(2026, 1, 15, tzinfo=UTC)
                outside = datetime(2026, 3, 1, tzinfo=UTC)
                await _ledger(conn, "proj-a", alice, "reserved", "8", inside)
                await _ledger(conn, "proj-a", bob, "reserved", "3", inside)
                await _ledger(conn, "proj-a", alice, "reserved", "100", outside)
            ctx = _ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            window = ["2026-01-10T00:00:00+00:00", "2026-02-01T00:00:00+00:00"]
            resp = await report_granted_set(pool, ctx, group_by="principal", window=window)
        assert resp.status == "ok"
        by_principal = {r["principal"]: r for r in _rows(resp)}
        assert by_principal["alice"]["reserved"] == "8.0000"
        assert by_principal["bob"]["reserved"] == "3.0000"

    asyncio.run(_run())


def test_group_by_principal_all_projects(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await report_all_projects(pool, ctx, group_by="principal")
        assert resp.status == "ok"
        keyed = {(r["project"], r["principal"]): r for r in _rows(resp)}
        assert keyed[("proj-a", "alice")]["reserved"] == "10.0000"
        assert keyed[("proj-b", "bob")]["reserved"] == "20.0000"

    asyncio.run(_run())


# ---- input validation -------------------------------------------------------------


def test_invalid_group_by_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await report_granted_set(pool, _ctx(), group_by="project")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.suggested_next_actions == ["accounting.report_granted_set"]

    asyncio.run(_run())


def test_invalid_window_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await report_granted_set(pool, _ctx(), window=["not-a-date", None])
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.suggested_next_actions == ["accounting.report_granted_set"]

    asyncio.run(_run())


def test_naive_window_bound_is_config_error(migrated_url: str) -> None:
    # ledger.ts is timestamptz; a tz-naive bound must fail closed, not compare in an
    # unintended zone.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await report_granted_set(pool, _ctx(), window=["2026-01-01T00:00:00", None])
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_inverted_window_is_config_error(migrated_url: str) -> None:
    # start >= end must error rather than return a silently-empty rollup.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await report_granted_set(
                pool,
                _ctx(),
                window=["2026-02-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"],
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_granted_set_explicit_empty_list_is_empty_rollup_unaudited(migrated_url: str) -> None:
    # Naming an explicit empty list resolves to zero projects: an empty rollup (success),
    # no audit row (distinct from the default-to-ctx.projects path).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",))
            resp = await report_granted_set(pool, ctx, projects=[])
        assert resp.status == "ok"
        assert _rows(resp) == []
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_all_projects_universe_includes_ledger_without_budget(migrated_url: str) -> None:
    # The oversight read must span every project: a project with ledger spend but no budget
    # row is still reported (not dropped from the cross-tenant total).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                # proj-x has ledger rows but NO budget row.
                x = await _alloc(conn, res, "proj-x", "xavier")
                await _ledger(conn, "proj-x", x, "reserved", "42")
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await report_all_projects(pool, ctx)
        assert resp.status == "ok"
        by_project = {r["project"]: r for r in _rows(resp)}
        assert by_project["proj-x"]["reserved"] == "42.0000"

    asyncio.run(_run())
