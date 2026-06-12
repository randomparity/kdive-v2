"""The denial chokepoint records the audit `actor` (boundary-test prerequisite, ADR-0089).

`audit_platform_denial` is the shared writer for every recorded platform-role denial. A
denial from the operator CLI (client id ``kdivectl``) must land an ``operator-cli`` row;
an agent-session denial lands ``agent``; an unrecognised caller lands ``unknown``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools._platform_auth import audit_platform_denial
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole

_CLI_CLIENT_ID = "kdivectl"


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _ctx(*, client_id: str | None, agent_session: str | None) -> RequestContext:
    return RequestContext(
        principal="op-1",
        agent_session=agent_session,
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
        client_id=client_id,
    )


async def _actor_for_tool(pool: AsyncConnectionPool, tool: str) -> object:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT actor FROM platform_audit_log WHERE tool = %s", (tool,))
        row = await cur.fetchone()
    assert row is not None
    return row[0]


def test_platform_denial_records_operator_cli_actor(migrated_url: str) -> None:
    async def _run() -> object:
        async with _pool(migrated_url) as pool:
            await audit_platform_denial(
                pool,
                _ctx(client_id=_CLI_CLIENT_ID, agent_session=None),
                tool="ops.force_release",
                scope="all-projects",
            )
            return await _actor_for_tool(pool, "ops.force_release")

    assert asyncio.run(_run()) == "operator-cli"


def test_platform_denial_records_agent_actor(migrated_url: str) -> None:
    async def _run() -> object:
        async with _pool(migrated_url) as pool:
            await audit_platform_denial(
                pool,
                _ctx(client_id=None, agent_session="sess-9"),
                tool="ops.agent_denied",
                scope="all-projects",
            )
            return await _actor_for_tool(pool, "ops.agent_denied")

    assert asyncio.run(_run()) == "agent"


def test_platform_denial_records_unknown_actor_for_stray_caller(migrated_url: str) -> None:
    async def _run() -> object:
        async with _pool(migrated_url) as pool:
            await audit_platform_denial(
                pool,
                _ctx(client_id="mystery", agent_session=None),
                tool="ops.mystery_denied",
                scope="all-projects",
            )
            return await _actor_for_tool(pool, "ops.mystery_denied")

    assert asyncio.run(_run()) == "unknown"
