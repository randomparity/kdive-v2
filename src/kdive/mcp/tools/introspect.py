"""The `introspect.from_vmcore` MCP tool: offline drgn introspection of a captured vmcore.

`introspect.from_vmcore(run_id)` is a synchronous, ungated offline read (ADR-0033). It
resolves the Run's `debuginfo_ref` (the build-plane `vmlinux`), the build plane's recorded
`build_id` (provenance), and the Run's System's captured raw `vmcore` key — the same
resolution shape `vmcore.py`'s postmortem path uses, replicated here so this plane stays off
`vmcore.py`. It then runs the `VmcoreIntrospector` port over the captured core and returns
the **already-redacted** report (the port is the single redaction boundary, ADR-0033 §6) as
a JSON string in `data["report"]`.
"""

from __future__ import annotations

import asyncio
import json
from typing import LiteralString, NamedTuple
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import DEBUG_SESSIONS, RUNS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.state import DebugSessionState
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.providers.local_libvirt.introspect_drgn import (
    LiveIntrospector,
    LocalLibvirtLiveIntrospect,
    LocalLibvirtVmcoreIntrospect,
    VmcoreIntrospector,
)
from kdive.security.rbac import Role, require_role

# The fixed live-helper set (ADR-0033 §2 / ADR-0039 §3): the same three in-tree helpers as the
# offline path. There is no caller-supplied drgn script — an unknown helper is rejected.
_LIVE_HELPERS = frozenset({"tasks", "modules", "sysinfo"})
_SSH = "ssh"

_RAW_KEY_SQL: LiteralString = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND object_key LIKE %s"
)
_BUILD_STEP_SQL: LiteralString = "SELECT result FROM run_steps WHERE run_id = %s AND step = 'build'"


def _config_error(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR)


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


class _Targets(NamedTuple):
    """The resolved (non-null) inputs the introspection port needs to open the core."""

    debuginfo_ref: str
    build_id: str
    vmcore_ref: str


async def _build_id_for_run(conn: AsyncConnection, run_id: UUID) -> str | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_BUILD_STEP_SQL, (run_id,))
        row = await cur.fetchone()
    if row is None or not isinstance(row["result"], dict):
        return None
    build_id = row["result"].get("build_id")
    return build_id if isinstance(build_id, str) and build_id else None


async def _resolve(
    conn: AsyncConnection, ctx: RequestContext, run_id: str
) -> _Targets | ToolResponse:
    """Resolve the Run's debuginfo ref, recorded build-id, and captured core key, or a failure."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    run = await RUNS.get(conn, uid)
    if run is None or run.project not in ctx.projects:
        return _config_error(run_id)
    if run.debuginfo_ref is None:
        return _config_error(run_id)
    build_id = await _build_id_for_run(conn, uid)
    if build_id is None:
        return _config_error(run_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RAW_KEY_SQL, (run.system_id, "%/vmcore"))
        row = await cur.fetchone()
    if row is None:
        return _config_error(run_id)
    return _Targets(run.debuginfo_ref, build_id, str(row["object_key"]))


async def introspect_from_vmcore(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    introspector: VmcoreIntrospector,
) -> ToolResponse:
    """Run offline drgn introspection over the Run's captured core; return the redacted report.

    Ungated. A Run with a null `debuginfo_ref`, no recorded `build` step, or a System with no
    captured core is a `configuration_error`; a provenance mismatch or a drgn open/decode fault
    surfaces as the port's typed `CategorizedError` category, never a 500.
    """
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resolved = await _resolve(conn, ctx, run_id)
        if isinstance(resolved, ToolResponse):
            return resolved
        try:
            output = await asyncio.to_thread(
                introspector.from_vmcore,
                vmcore_ref=resolved.vmcore_ref,
                debuginfo_ref=resolved.debuginfo_ref,
                expected_build_id=resolved.build_id,
            )
        except CategorizedError as exc:
            return ToolResponse.failure(run_id, exc.category)
        report = json.dumps(
            {"tasks": output.tasks, "modules": output.modules, "sysinfo": output.sysinfo}
        )
        return ToolResponse.success(
            run_id,
            "succeeded",
            suggested_next_actions=["introspect.from_vmcore", "artifacts.list"],
            data={"report": report, "truncated": str(output.truncated).lower()},
        )


async def _live_ssh_session(
    conn: AsyncConnection, ctx: RequestContext, session_id: str
) -> tuple[str, str] | ToolResponse:
    """Resolve a `live` ssh DebugSession, returning its (project, transport_handle), or a failure.

    Gates on UUID shape, project membership, ``operator`` role, ``live`` state, and an ``ssh``
    transport (a live `introspect.run` requires the ssh transport, not gdbstub; ADR-0039 §4).
    """
    uid = _as_uuid(session_id)
    if uid is None:
        return _config_error(session_id)
    session = await DEBUG_SESSIONS.get(conn, uid)
    if session is None or session.project not in ctx.projects:
        return _config_error(session_id)
    require_role(ctx, session.project, Role.OPERATOR)
    if session.state is not DebugSessionState.LIVE or session.transport != _SSH:
        return _config_error(session_id)
    if session.transport_handle is None:
        return _config_error(session_id)
    return session.project, session.transport_handle


async def introspect_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    session_id: str,
    helper: str,
    introspector: LiveIntrospector,
) -> ToolResponse:
    """Run live drgn introspection over a `live` ssh DebugSession; return the redacted report.

    Requires a `live` ssh DebugSession (operator). The ``helper`` must be one of the fixed
    in-tree helpers — there is no caller-supplied drgn script. The port is the single redaction
    boundary, so the returned report is already masked; the raw drgn-over-ssh transcript is
    ``sensitive`` and is never returned (the response only advertises that, ADR-0039 §2/§3).
    """
    if helper not in _LIVE_HELPERS:
        return _config_error(session_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resolved = await _live_ssh_session(conn, ctx, session_id)
        if isinstance(resolved, ToolResponse):
            return resolved
        _project, transport_handle = resolved
        try:
            output = await asyncio.to_thread(introspector.run, transport_handle=transport_handle)
        except CategorizedError as exc:
            return ToolResponse.failure(session_id, exc.category)
        report = json.dumps(
            {"tasks": output.tasks, "modules": output.modules, "sysinfo": output.sysinfo}
        )
        return ToolResponse.success(
            session_id,
            "succeeded",
            suggested_next_actions=["introspect.run", "debug.end_session"],
            data={
                "report": report,
                "truncated": str(output.truncated).lower(),
                "transcript_sensitivity": "sensitive",
            },
        )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `introspect.from_vmcore` and `introspect.run` tools on ``app``."""
    introspector = LocalLibvirtVmcoreIntrospect.from_env()
    live_introspector = LocalLibvirtLiveIntrospect.from_env()

    @app.tool(name="introspect.from_vmcore")
    async def introspect_from_vmcore_tool(run_id: str) -> ToolResponse:
        return await introspect_from_vmcore(
            pool, current_context(), run_id=run_id, introspector=introspector
        )

    @app.tool(name="introspect.run")
    async def introspect_run_tool(session_id: str, helper: str) -> ToolResponse:
        return await introspect_run(
            pool,
            current_context(),
            session_id=session_id,
            helper=helper,
            introspector=live_introspector,
        )
