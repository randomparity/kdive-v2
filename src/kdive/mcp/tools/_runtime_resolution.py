"""Shared provider runtime resolution helpers for MCP tool wrappers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid, config_error
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProviderRuntime

type _RuntimeResolver = Callable[[AsyncConnection, UUID], Awaitable[ProviderRuntime]]


async def _runtime_for_object(
    pool: AsyncConnectionPool,
    object_id: str,
    resolve: _RuntimeResolver,
) -> ProviderRuntime | ToolResponse:
    uid = as_uuid(object_id)
    if uid is None:
        return config_error(object_id)
    async with pool.connection() as conn:
        try:
            return await resolve(conn, uid)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(object_id, exc)


async def runtime_for_allocation(
    pool: AsyncConnectionPool, resolver: ProviderResolver, allocation_id: str
) -> ProviderRuntime | ToolResponse:
    return await _runtime_for_object(pool, allocation_id, resolver.runtime_for_allocation)


async def runtime_for_system(
    pool: AsyncConnectionPool, resolver: ProviderResolver, system_id: str
) -> ProviderRuntime | ToolResponse:
    return await _runtime_for_object(pool, system_id, resolver.runtime_for_system)


async def runtime_for_run(
    pool: AsyncConnectionPool, resolver: ProviderResolver, run_id: str
) -> ProviderRuntime | ToolResponse:
    return await _runtime_for_object(pool, run_id, resolver.runtime_for_run)
