"""FastMCP wrapper-boundary tests for representative catalog and lifecycle tools."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import BUDGETS, QUOTAS
from kdive.domain.models import Budget, Quota
from kdive.mcp.app import build_app
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog import resources as resources_tools
from kdive.mcp.tools.lifecycle import allocations as allocations_tools
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role
from kdive.services.resource_discovery import register_discovered_resource
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _verifier() -> JWTVerifier:
    keypair = make_keypair()
    return JWTVerifier(public_key=keypair.public_key, issuer=ISSUER, audience=AUDIENCE)


def _ctx() -> RequestContext:
    return RequestContext(
        principal="wrapper-user",
        agent_session="wrapper-session",
        projects=("proj",),
        roles={"proj": Role.OPERATOR},
        platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_resource_and_limits(pool: AsyncConnectionPool) -> str:
    discovery = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=2,
    )
    async with pool.connection() as conn:
        resource = await register_discovered_resource(
            conn,
            discovery.list_resources()[0],
            pool="local-libvirt",
            cost_class="local",
        )
        await BUDGETS.upsert(
            conn,
            Budget(
                project="proj",
                limit_kcu=Decimal("1000000"),
                spent_kcu=Decimal(0),
                updated_at=_DT,
            ),
        )
        await QUOTAS.upsert(
            conn,
            Quota(
                project="proj",
                max_concurrent_allocations=10,
                max_concurrent_systems=10,
                updated_at=_DT,
            ),
        )
    return str(resource.id)


async def _call_tool(client: Client, name: str, args: dict[str, Any] | None = None) -> ToolResponse:
    result = await client.call_tool(name, args or {}, raise_on_error=False)
    assert not getattr(result, "is_error", False)
    payload = result.structured_content
    assert isinstance(payload, dict)
    return ToolResponse.model_validate(payload)


def test_catalog_resource_wrappers_roundtrip_through_fastmcp(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resource_id = await _seed_resource_and_limits(pool)
            monkeypatch.setattr(resources_tools, "current_context", _ctx)
            app = build_app(pool, verifier=_verifier())
            async with Client(app) as client:
                listed = await _call_tool(client, "resources.list")
                cordoned = await _call_tool(
                    client, "resources.cordon", {"resource_id": resource_id}
                )
            async with pool.connection() as conn:
                row = await conn.execute(
                    "SELECT cordoned FROM resources WHERE id = %s", (UUID(resource_id),)
                )
                cordoned_state = await row.fetchone()

        assert listed.object_id == "resources"
        assert listed.status == "ok"
        assert listed.items[0].object_id == resource_id
        assert listed.items[0].data["kind"] == "local-libvirt"
        assert cordoned.object_id == resource_id
        assert cordoned.status == "available"
        assert cordoned_state == (True,)

    asyncio.run(_run())


def test_lifecycle_allocation_wrappers_roundtrip_through_fastmcp(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> tuple[ToolResponse, ToolResponse]:
        async with _pool(migrated_url) as pool:
            await _seed_resource_and_limits(pool)
            monkeypatch.setattr(allocations_tools, "current_context", _ctx)
            app = build_app(pool, verifier=_verifier())
            async with Client(app) as client:
                granted = await _call_tool(
                    client,
                    "allocations.request",
                    {
                        "project": "proj",
                        "request": {
                            "vcpus": 1,
                            "memory_gb": 1,
                            "disk_gb": 10,
                            "resource": {"mode": "kind", "kind": "local-libvirt"},
                        },
                    },
                )
                fetched = await _call_tool(
                    client, "allocations.get", {"allocation_id": granted.object_id}
                )
        return granted, fetched

    granted, fetched = asyncio.run(_run())
    assert granted.status == "granted"
    assert granted.data["project"] == "proj"
    assert fetched.object_id == granted.object_id
    assert fetched.status == "granted"
    assert fetched.data["project"] == "proj"
