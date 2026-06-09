"""Read-side `runs.get` MCP handler."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import RUNS, SYSTEMS
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.lifecycle.runs.common import envelope_for_run
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.steps import install_method_for as _install_method_for
from kdive.services.runs.steps import system_required_cmdline


async def get_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Return a Run the caller's project owns, advertising the boot's required cmdline."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.VIEWER)
            system = await SYSTEMS.get(conn, run.system_id)
        required = (
            system_required_cmdline(_install_method_for(system)) if system is not None else None
        )
        return envelope_for_run(run, required_cmdline=required)
