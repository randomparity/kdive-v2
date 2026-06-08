"""`audit.query` auditor-read tool tests — project + cross-project forms (#141).

The handler is called directly with an injected pool + RequestContext (the repo's unit
contract). Coverage maps to the #141 acceptance bullets:

* project form: requires ``admin`` on the named project; a viewer/operator on it is
  denied; a non-member is denied; the read is **not** written to ``platform_audit_log``
  (a per-project admin reading their own trail is not a cross-project oversight read).
* cross-project form (no ``project`` filter): requires ``platform_auditor`` (satisfied by
  ``platform_admin``); a project-only token is denied; every served read writes exactly
  one ``platform_audit_log`` row and **no** ``audit_log`` row (never pollutes the trail
  it inspects).
* filters: principal / object / time window / transition narrow the returned rows in
  both forms; a malformed window fails closed (configuration_error).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.ops import audit as audit_tools
from kdive.security.authz.rbac import PlatformRole, Role, RoleDenied

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


async def _audit_row(
    conn: psycopg.AsyncConnection,
    *,
    project: str,
    principal: str,
    tool: str,
    transition: str,
    object_kind: str,
    object_id: UUID,
    ts: datetime,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO audit_log "
            "(ts, principal, agent_session, project, tool, object_kind, object_id, "
            " transition, args_digest) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (ts, principal, "sess-x", project, tool, object_kind, object_id, transition, "d"),
        )


async def _seed_two_projects(pool: AsyncConnectionPool) -> dict[str, UUID]:
    """Seed audit_log rows for proj-a (alice) and proj-b (bob), distinct transitions/times."""
    ids = {"a1": uuid4(), "a2": uuid4(), "b1": uuid4()}
    async with pool.connection() as conn, conn.transaction():
        await _audit_row(
            conn,
            project="proj-a",
            principal="alice",
            tool="allocations.request",
            transition="requested",
            object_kind="allocation",
            object_id=ids["a1"],
            ts=_DT,
        )
        await _audit_row(
            conn,
            project="proj-a",
            principal="alice",
            tool="systems.create",
            transition="defined",
            object_kind="system",
            object_id=ids["a2"],
            ts=_DT + timedelta(hours=2),
        )
        await _audit_row(
            conn,
            project="proj-b",
            principal="bob",
            tool="allocations.request",
            transition="requested",
            object_kind="allocation",
            object_id=ids["b1"],
            ts=_DT + timedelta(hours=1),
        )
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


def _rows(resp: ToolResponse) -> list[dict[str, object]]:
    return json.loads(resp.data["rows"])


def _project_query(
    project: str,
    *,
    principal: str | None = None,
    object_id: str | None = None,
    transition: str | None = None,
    window: list[str | None] | None = None,
) -> audit_tools.ProjectAuditQuery:
    return audit_tools.ProjectAuditQuery(
        scope="project",
        project=project,
        principal=principal,
        object_id=object_id,
        transition=transition,
        window=window,
    )


def _all_projects_query(
    *,
    principal: str | None = None,
    object_id: str | None = None,
    transition: str | None = None,
    window: list[str | None] | None = None,
) -> audit_tools.AllProjectsAuditQuery:
    return audit_tools.AllProjectsAuditQuery(
        scope="all-projects",
        principal=principal,
        object_id=object_id,
        transition=transition,
        window=window,
    )


# ---- project form -----------------------------------------------------------------


def test_project_form_admin_reads_only_that_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await audit_tools.query_project(pool, ctx, request=_project_query("proj-a"))
        assert resp.status == "ok"
        assert resp.error_category is None
        projects = {r["project"] for r in _rows(resp)}
        assert projects == {"proj-a"}
        assert len(_rows(resp)) == 2
        # A per-project admin read of its own trail is NOT a platform oversight read.
        assert await _count_platform_audit(migrated_url) == 0
        # And it writes no audit_log row (a read, not a transition).
        assert await _count_audit_log(migrated_url) == 3

    asyncio.run(_run())


def test_project_form_member_below_admin_propagates_roledenied(migrated_url: str) -> None:
    # A member of the project whose held rank is below admin (viewer/operator) is a
    # member-over-reach: post-#142 require_role raises RoleDenied, and the project form must
    # let it propagate to DenialAuditMiddleware (ADR-0062 §8 — the boundary writes the
    # transition='denied' audit_log row) rather than swallowing it into a failure envelope.
    # A non-member instead gets the base AuthorizationError envelope (see
    # test_project_form_non_member_denied), so the two denial kinds stay distinct.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            for role in (Role.VIEWER, Role.OPERATOR):
                ctx = _ctx(roles={"proj-a": role}, projects=("proj-a",))
                with pytest.raises(RoleDenied) as exc:
                    await audit_tools.query_project(pool, ctx, request=_project_query("proj-a"))
                assert exc.value.project == "proj-a"
            # The boundary writes to audit_log, not platform_audit_log; the tool writes neither.
            assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_project_form_non_member_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            # admin on proj-a but querying proj-b (not a member) → denied.
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await audit_tools.query_project(pool, ctx, request=_project_query("proj-b"))
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"

    asyncio.run(_run())


def test_project_form_platform_auditor_without_project_role_denied(migrated_url: str) -> None:
    # A platform_auditor naming a specific project does NOT inherit project admin: the
    # project form gates on require_role(project, admin), a separate axis.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await audit_tools.query_project(pool, ctx, request=_project_query("proj-a"))
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"

    asyncio.run(_run())


# ---- cross-project form -----------------------------------------------------------


def test_cross_project_auditor_reads_all_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await audit_tools.query_all_projects(pool, ctx, request=_all_projects_query())
        assert resp.status == "ok"
        assert {r["project"] for r in _rows(resp)} == {"proj-a", "proj-b"}
        assert len(_rows(resp)) == 3
        assert resp.data["count"] == "3"
        assert resp.data["truncated"] == "false"
        # Exactly one platform_audit_log row (role recorded), zero audit_log writes.
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("user-1", "platform_auditor", "audit.query", "all-projects")]
        assert await _count_audit_log(migrated_url) == 3  # only the seeded rows

    asyncio.run(_run())


def test_cross_project_admin_satisfies_auditor_gate(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}))
            resp = await audit_tools.query_all_projects(pool, ctx, request=_all_projects_query())
        assert resp.status == "ok"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_admin"

    asyncio.run(_run())


def test_cross_project_project_only_token_denied_unaudited(migrated_url: str) -> None:
    # A project-scoped admin holds no platform role → denied; the denial is NOT audited
    # (routine non-grant on an openly-callable read; no write amplification).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await audit_tools.query_all_projects(pool, ctx, request=_all_projects_query())
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert resp.suggested_next_actions == ["audit.query"]
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_cross_project_operator_denied_but_audited(migrated_url: str) -> None:
    # platform_operator does NOT satisfy the auditor gate, but holds a platform role, so
    # the over-reach denial IS audited (the accountability target).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))
            resp = await audit_tools.query_all_projects(pool, ctx, request=_all_projects_query())
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_operator"
        assert rows[0][3] == "all-projects"

    asyncio.run(_run())


def test_project_request_requires_explicit_scope() -> None:
    with pytest.raises(ValidationError):
        audit_tools.ProjectAuditQuery.model_validate({"project": "proj-a"})


def test_all_projects_request_rejects_project_filter() -> None:
    with pytest.raises(ValidationError):
        audit_tools.AllProjectsAuditQuery.model_validate(
            {"scope": "all-projects", "project": "proj-a"}
        )


# ---- filters ----------------------------------------------------------------------


def test_filter_by_principal_cross_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await audit_tools.query_all_projects(
                pool, ctx, request=_all_projects_query(principal="alice")
            )
        assert resp.status == "ok"
        assert {r["principal"] for r in _rows(resp)} == {"alice"}
        assert len(_rows(resp)) == 2

    asyncio.run(_run())


def test_filter_by_object_project_form(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ids = await _seed_two_projects(pool)
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await audit_tools.query_project(
                pool,
                ctx,
                request=_project_query("proj-a", object_id=str(ids["a2"])),
            )
        assert resp.status == "ok"
        rows = _rows(resp)
        assert len(rows) == 1
        assert rows[0]["object_id"] == str(ids["a2"])
        assert rows[0]["transition"] == "defined"

    asyncio.run(_run())


def test_filter_by_transition_cross_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await audit_tools.query_all_projects(
                pool, ctx, request=_all_projects_query(transition="defined")
            )
        assert resp.status == "ok"
        assert {r["transition"] for r in _rows(resp)} == {"defined"}
        assert len(_rows(resp)) == 1

    asyncio.run(_run())


def test_filter_by_window_cross_project(migrated_url: str) -> None:
    # Window [_DT+30m, _DT+90m] admits only the proj-b row at _DT+1h.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            window: list[str | None] = [
                (_DT + timedelta(minutes=30)).isoformat(),
                (_DT + timedelta(minutes=90)).isoformat(),
            ]
            resp = await audit_tools.query_all_projects(
                pool, ctx, request=_all_projects_query(window=window)
            )
        assert resp.status == "ok"
        rows = _rows(resp)
        assert len(rows) == 1
        assert rows[0]["project"] == "proj-b"

    asyncio.run(_run())


def test_malformed_object_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_two_projects(pool)
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await audit_tools.query_project(
                pool,
                ctx,
                request=_project_query("proj-a", object_id="not-a-uuid"),
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.suggested_next_actions == ["audit.query"]

    asyncio.run(_run())


def test_malformed_window_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await audit_tools.query_all_projects(
                pool, ctx, request=_all_projects_query(window=["not-a-date", None])
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_naive_window_bound_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await audit_tools.query_all_projects(
                pool,
                ctx,
                request=_all_projects_query(window=["2026-01-01T00:00:00", None]),
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())
