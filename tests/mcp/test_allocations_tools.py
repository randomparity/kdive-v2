"""allocations.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.models import Allocation
from kdive.domain.state import AllocationState, IllegalTransition
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import allocations as alloc_tools
from kdive.providers.local_libvirt.discovery import (
    LocalLibvirtDiscovery,
    register_local_libvirt_resource,
)
from kdive.security.rbac import AuthorizationError, Role
from tests.providers.local_libvirt.conftest import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _register(pool: AsyncConnectionPool, *, cap: int = 1) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=cap,
    )
    async with pool.connection() as conn:
        res = await register_local_libvirt_resource(
            conn, disc, pool="local-libvirt", cost_class="local"
        )
    return str(res.id)


async def _seed_alloc(pool: AsyncConnectionPool, resource_id: str, state: AllocationState) -> str:
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=UUID(resource_id),
                state=state,
            ),
        )
    return str(alloc.id)


def test_request_under_cap_grants(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            resp = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
        assert resp.status == "granted"
        assert resp.error_category is None
        assert resp.data["project"] == "proj"

    asyncio.run(_run())


def test_request_at_cap_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=1)
            await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            resp = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
        assert resp.status == "error"
        assert resp.error_category == "allocation_denied"
        assert resp.object_id == res_id
        assert resp.data["reason"] == "at_capacity"

    asyncio.run(_run())


def test_request_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            try:
                await alloc_tools.request_allocation(pool, _ctx(Role.VIEWER), project="proj")
                raise AssertionError("expected AuthorizationError")
            except AuthorizationError:
                pass

    asyncio.run(_run())


def test_request_no_resource_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_own_allocation_returns_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            resp = await alloc_tools.get_allocation(pool, _ctx(), req.object_id)
        assert resp.object_id == req.object_id
        assert resp.status == "granted"

    asyncio.run(_run())


def test_get_other_project_allocation_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            other = _ctx(projects=("elsewhere",), role=Role.OPERATOR)
            resp = await alloc_tools.get_allocation(pool, other, req.object_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_failed_allocation_renders_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.FAILED)
            resp = await alloc_tools.get_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert resp.data["current_status"] == "failed"

    asyncio.run(_run())


def test_release_granted_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            resp = await alloc_tools.release_allocation(pool, _ctx(), req.object_id)
            assert resp.status == "released"
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM audit_log WHERE object_id = %s", (req.object_id,)
                )
                row = await cur.fetchone()
            # ->granted (admission) + granted->releasing + releasing->released
            assert row is not None and row[0] == 3

    asyncio.run(_run())


def test_release_active_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.ACTIVE)
            resp = await alloc_tools.release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "released"

    asyncio.run(_run())


def test_release_terminal_allocation_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.RELEASED)
            resp = await alloc_tools.release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "released"

    asyncio.run(_run())


def test_release_requested_allocation_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.REQUESTED)
            resp = await alloc_tools.release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_release_illegal_transition_backstop_returns_failure(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the backstop: a state change slips past the locked re-read (a future
    # provision path could do this). update_state raising IllegalTransition must map to a
    # clean configuration_error envelope carrying the actual current state, not a 500.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.GRANTED)

            async def _boom(*args: object, **kwargs: object) -> object:
                raise IllegalTransition("forced")

            monkeypatch.setattr(ALLOCATIONS, "update_state", _boom)
            resp = await alloc_tools.release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "granted"  # re-read on a fresh connection

    asyncio.run(_run())


def test_list_returns_project_allocations(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=3)
            await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            await alloc_tools.request_allocation(pool, _ctx(), project="proj")
            responses = await alloc_tools.list_allocations(pool, _ctx(), project="proj", limit=50)
        assert len(responses) == 2
        assert all(r.status == "granted" for r in responses)

    asyncio.run(_run())
