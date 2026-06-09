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
type RuntimeHandler = Callable[[ProviderRuntime], Awaitable[ToolResponse]]


class _InvalidRuntimeObjectId(ValueError):
    def __init__(self, object_id: str) -> None:
        super().__init__(f"invalid provider runtime object id: {object_id}")
        self.object_id = object_id


async def _runtime_for_object(
    pool: AsyncConnectionPool,
    object_id: str,
    resolve: _RuntimeResolver,
) -> ProviderRuntime:
    uid = as_uuid(object_id)
    if uid is None:
        raise _InvalidRuntimeObjectId(object_id)
    async with pool.connection() as conn:
        return await resolve(conn, uid)


async def _with_runtime_for_object(
    pool: AsyncConnectionPool,
    object_id: str,
    resolve: _RuntimeResolver,
    handle: RuntimeHandler,
) -> ToolResponse:
    try:
        runtime = await _runtime_for_object(pool, object_id, resolve)
    except _InvalidRuntimeObjectId:
        return config_error(object_id)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(object_id, exc)
    return await handle(runtime)


async def runtime_for_allocation(
    pool: AsyncConnectionPool, resolver: ProviderResolver, allocation_id: str
) -> ProviderRuntime:
    return await _runtime_for_object(pool, allocation_id, resolver.runtime_for_allocation)


async def runtime_for_system(
    pool: AsyncConnectionPool, resolver: ProviderResolver, system_id: str
) -> ProviderRuntime:
    return await _runtime_for_object(pool, system_id, resolver.runtime_for_system)


async def runtime_for_run(
    pool: AsyncConnectionPool, resolver: ProviderResolver, run_id: str
) -> ProviderRuntime:
    return await _runtime_for_object(pool, run_id, resolver.runtime_for_run)


async def with_runtime_for_allocation(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    allocation_id: str,
    handle: RuntimeHandler,
) -> ToolResponse:
    return await _with_runtime_for_object(
        pool, allocation_id, resolver.runtime_for_allocation, handle
    )


async def with_runtime_for_system(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    system_id: str,
    handle: RuntimeHandler,
) -> ToolResponse:
    return await _with_runtime_for_object(pool, system_id, resolver.runtime_for_system, handle)


async def with_runtime_for_run(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    run_id: str,
    handle: RuntimeHandler,
) -> ToolResponse:
    return await _with_runtime_for_object(pool, run_id, resolver.runtime_for_run, handle)
