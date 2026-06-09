"""Tests for shared Run -> vmcore target resolution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools._vmcore_targets import (
    RunVmcoreTarget,
    resolve_run_vmcore_target,
)
from kdive.security.authz.rbac import AuthorizationError, Role
from tests.mcp._seed import seed_crashed_system, seed_run_on_system


def _ctx(
    role: Role | None = Role.VIEWER, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_vmcore_row(pool: AsyncConnectionPool, system_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('systems', %s, %s, 'e', 'sensitive', 'vmcore')",
            (system_id, f"local/systems/{system_id}/vmcore-host_dump"),
        )


async def _built_run_with_core(pool: AsyncConnectionPool) -> str:
    system_id = await seed_crashed_system(pool)
    run_id = await seed_run_on_system(
        pool,
        system_id,
        debuginfo_ref="k/runs/r/vmlinux",
        build_id="deadbeef",
    )
    await _seed_vmcore_row(pool, system_id)
    return run_id


def test_resolve_run_vmcore_target_returns_port_inputs(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            async with pool.connection() as conn:
                resolved = await resolve_run_vmcore_target(conn, _ctx(), run_id)

        assert isinstance(resolved, RunVmcoreTarget)
        assert resolved.debuginfo_ref == "k/runs/r/vmlinux"
        assert resolved.build_id == "deadbeef"
        assert resolved.vmcore_ref.endswith("/vmcore-host_dump")

    asyncio.run(_run())


def test_resolve_run_vmcore_target_rejects_bad_run_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool, pool.connection() as conn:
            with pytest.raises(CategorizedError) as exc:
                await resolve_run_vmcore_target(conn, _ctx(), "not-a-uuid")

        assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())


def test_resolve_run_vmcore_target_requires_recorded_build_id(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool,
                system_id,
                debuginfo_ref="k/runs/r/vmlinux",
                build_id=None,
            )
            await _seed_vmcore_row(pool, system_id)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await resolve_run_vmcore_target(conn, _ctx(), run_id)

        assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())


def test_resolve_run_vmcore_target_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            async with pool.connection() as conn:
                with pytest.raises(AuthorizationError):
                    await resolve_run_vmcore_target(conn, _ctx(role=None), run_id)

    asyncio.run(_run())
