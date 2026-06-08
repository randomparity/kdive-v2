"""Shared Run → build → vmcore target resolution for MCP read tools."""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.artifact_queries import raw_vmcore_key
from kdive.db.repositories import RUNS
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.run_steps import existing_build_result


class RunVmcoreTarget(NamedTuple):
    """The resolved inputs needed to analyze a Run's captured vmcore."""

    debuginfo_ref: str
    build_id: str
    vmcore_ref: str


async def resolve_run_vmcore_target(
    conn: AsyncConnection, ctx: RequestContext, run_id: str
) -> RunVmcoreTarget | ToolResponse:
    """Resolve debuginfo ref, build-id, and raw vmcore key for a viewer-authorized Run."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    run = await RUNS.get(conn, uid)
    if run is None or run.project not in ctx.projects:
        return _config_error(run_id)
    require_role(ctx, run.project, Role.VIEWER)
    if run.debuginfo_ref is None:
        return _config_error(run_id)
    build_id = await _build_id_for_run(conn, uid)
    if build_id is None:
        return _config_error(run_id)
    vmcore_ref = await raw_vmcore_key(conn, run.system_id)
    if vmcore_ref is None:
        return _config_error(run_id)
    return RunVmcoreTarget(run.debuginfo_ref, build_id, vmcore_ref)


async def _build_id_for_run(conn: AsyncConnection, run_id: UUID) -> str | None:
    result = await existing_build_result(conn, run_id)
    return None if result is None else result.build_id
