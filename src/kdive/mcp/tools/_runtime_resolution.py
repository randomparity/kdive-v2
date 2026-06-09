"""Shared provider runtime resolution helpers for MCP tool wrappers."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid, config_error
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProviderRuntime


async def runtime_for_run(
    pool: AsyncConnectionPool, resolver: ProviderResolver, run_id: str
) -> ProviderRuntime | ToolResponse:
    uid = as_uuid(run_id)
    if uid is None:
        return config_error(run_id)
    async with pool.connection() as conn:
        try:
            return await resolver.runtime_for_run(conn, uid)
        except CategorizedError as exc:
            return ToolResponse.failure(run_id, exc.category)
