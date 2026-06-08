"""Shared platform authorization audit helper tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.ops._auth import ALL_PROJECTS_SCOPE, audit_platform_denial, held_platform_roles
from kdive.security.audit import args_digest
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _ctx(*, platform_roles: frozenset[PlatformRole] = frozenset()) -> RequestContext:
    return RequestContext(
        principal="op-1",
        agent_session="sess-1",
        projects=(),
        roles={},
        platform_roles=platform_roles,
    )


def test_held_platform_roles_returns_sorted_values_or_none() -> None:
    assert held_platform_roles(_ctx()) is None
    assert (
        held_platform_roles(
            _ctx(
                platform_roles=frozenset(
                    {PlatformRole.PLATFORM_OPERATOR, PlatformRole.PLATFORM_AUDITOR}
                )
            )
        )
        == "platform_auditor,platform_operator"
    )


def test_audit_platform_denial_skips_project_only_callers(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await audit_platform_denial(
                pool,
                _ctx(),
                tool="ops.test",
                scope=ALL_PROJECTS_SCOPE,
            )
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT count(*) FROM platform_audit_log")
                row = await cur.fetchone()
        assert row == (0,)

    asyncio.run(_run())


def test_audit_platform_denial_records_held_roles_and_default_args(
    migrated_url: str,
) -> None:
    async def _run() -> tuple[object, ...] | None:
        async with _pool(migrated_url) as pool:
            await audit_platform_denial(
                pool,
                _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR})),
                tool="ops.test",
                scope=ALL_PROJECTS_SCOPE,
            )
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT principal, agent_session, platform_role, tool, scope, args_digest "
                    "FROM platform_audit_log"
                )
                return await cur.fetchone()

    assert asyncio.run(_run()) == (
        "op-1",
        "sess-1",
        "platform_auditor",
        "ops.test",
        ALL_PROJECTS_SCOPE,
        args_digest({}),
    )
