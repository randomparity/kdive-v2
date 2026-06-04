"""accounting.set_budget / set_quota handler tests — admin-gated project administration.

Handlers called directly with an injected pool (ADR-0007 §6). Setting a project's budget
or quota is ``admin``; a re-set of the budget preserves the DB-maintained ``spent_kcu``
running total; a missing-but-required field or a malformed value fails closed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import BUDGETS, QUOTAS
from kdive.mcp.auth import AuthError, RequestContext
from kdive.mcp.tools import accounting as acct_tools
from kdive.security.rbac import AuthorizationError, Role


def _ctx(
    role: Role | None = Role.ADMIN, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="admin-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def test_set_budget_creates_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await acct_tools.set_budget(pool, _ctx(), project="proj", limit_kcu="250")
            assert resp.status == "ok"
            assert resp.data["project"] == "proj"
            assert resp.data["limit_kcu"] == "250"
            async with pool.connection() as conn:
                budget = await BUDGETS.get(conn, "proj")
            assert budget is not None
            assert budget.limit_kcu == Decimal("250")
            assert budget.spent_kcu == Decimal(0)

    asyncio.run(_run())


def test_set_budget_reset_preserves_spent(migrated_url: str) -> None:
    # A re-set updates limit_kcu but must not clobber the DB-maintained spent_kcu total.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await acct_tools.set_budget(pool, _ctx(), project="proj", limit_kcu="100")
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE budgets SET spent_kcu = %s WHERE project = %s", (Decimal("42"), "proj")
                )
            await acct_tools.set_budget(pool, _ctx(), project="proj", limit_kcu="500")
            async with pool.connection() as conn:
                budget = await BUDGETS.get(conn, "proj")
            assert budget is not None
            assert budget.limit_kcu == Decimal("500")
            assert budget.spent_kcu == Decimal("42")  # preserved

    asyncio.run(_run())


def test_set_budget_requires_admin(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            with pytest.raises(AuthorizationError):
                await acct_tools.set_budget(
                    pool, _ctx(role=Role.OPERATOR), project="proj", limit_kcu="10"
                )

    asyncio.run(_run())


def test_set_budget_foreign_project_refused(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            other = _ctx(projects=("elsewhere",), role=Role.ADMIN)
            with pytest.raises(AuthError):
                await acct_tools.set_budget(pool, other, project="proj", limit_kcu="10")

    asyncio.run(_run())


@pytest.mark.parametrize("bad", ["-5", "not-a-number", "NaN", "Infinity"])
def test_set_budget_malformed_limit_is_config_error(migrated_url: str, bad: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await acct_tools.set_budget(pool, _ctx(), project="proj", limit_kcu=bad)
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            async with pool.connection() as conn:
                budget = await BUDGETS.get(conn, "proj")
            assert budget is None  # no row written on a malformed value

    asyncio.run(_run())


def test_set_quota_creates_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await acct_tools.set_quota(
                pool, _ctx(), project="proj", max_concurrent_allocations=3, max_concurrent_systems=5
            )
            assert resp.status == "ok"
            assert resp.data["max_concurrent_allocations"] == "3"
            assert resp.data["max_concurrent_systems"] == "5"
            async with pool.connection() as conn:
                quota = await QUOTAS.get(conn, "proj")
            assert quota is not None
            assert quota.max_concurrent_allocations == 3
            assert quota.max_concurrent_systems == 5

    asyncio.run(_run())


def test_set_quota_reset_overwrites(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await acct_tools.set_quota(
                pool, _ctx(), project="proj", max_concurrent_allocations=1, max_concurrent_systems=1
            )
            await acct_tools.set_quota(
                pool,
                _ctx(),
                project="proj",
                max_concurrent_allocations=9,
                max_concurrent_systems=7,
            )
            async with pool.connection() as conn:
                quota = await QUOTAS.get(conn, "proj")
            assert quota is not None
            assert quota.max_concurrent_allocations == 9
            assert quota.max_concurrent_systems == 7

    asyncio.run(_run())


def test_set_quota_requires_admin(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            with pytest.raises(AuthorizationError):
                await acct_tools.set_quota(
                    pool,
                    _ctx(role=Role.OPERATOR),
                    project="proj",
                    max_concurrent_allocations=1,
                    max_concurrent_systems=1,
                )

    asyncio.run(_run())


@pytest.mark.parametrize(("allocs", "systems"), [(-1, 1), (1, -1)])
def test_set_quota_negative_is_config_error(migrated_url: str, allocs: int, systems: int) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await acct_tools.set_quota(
                pool,
                _ctx(),
                project="proj",
                max_concurrent_allocations=allocs,
                max_concurrent_systems=systems,
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            async with pool.connection() as conn:
                quota = await QUOTAS.get(conn, "proj")
            assert quota is None

    asyncio.run(_run())
