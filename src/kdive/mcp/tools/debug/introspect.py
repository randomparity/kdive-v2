"""The `introspect.from_vmcore` MCP tool: offline drgn introspection of a captured vmcore.

`introspect.from_vmcore(run_id)` is a synchronous offline viewer read (ADR-0033). It resolves
the Run's `debuginfo_ref` (the build-plane `vmlinux`), the build plane's recorded `build_id`
(provenance), and the Run's System's captured raw `vmcore` key through the shared
`mcp.tools._vmcore_targets` helper. It then runs the `VmcoreIntrospector` port and returns the
**already-redacted** report (the port is the single redaction boundary, ADR-0033 §6) as
structured data in `data["report"]`.
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import DEBUG_SESSIONS
from kdive.domain.errors import CategorizedError
from kdive.domain.state import DebugSessionState
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run
from kdive.mcp.tools._vmcore_targets import resolve_run_vmcore_target
from kdive.providers.ports import LiveIntrospector, VmcoreIntrospector
from kdive.providers.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role

# The fixed live-helper set (ADR-0033 §2 / ADR-0039 §3): the same three in-tree helpers as the
# offline path. There is no caller-supplied drgn script — an unknown helper is rejected.
_LIVE_HELPERS = frozenset({"tasks", "modules", "sysinfo"})
_SSH = "ssh"


async def introspect_from_vmcore(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    introspector: VmcoreIntrospector,
) -> ToolResponse:
    """Run offline drgn introspection over the Run's captured core; return the redacted report.

    Requires the viewer role. A Run with a null `debuginfo_ref`, no recorded `build` step, or a
    System with no captured core is a `configuration_error`; a provenance mismatch or a drgn
    open/decode fault surfaces as the port's typed `CategorizedError` category, never a 500.
    """
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resolved = await resolve_run_vmcore_target(conn, ctx, run_id)
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
            return ToolResponse.failure_from_error(run_id, exc)
        report = {"tasks": output.tasks, "modules": output.modules, "sysinfo": output.sysinfo}
        return ToolResponse.success(
            run_id,
            "succeeded",
            suggested_next_actions=["introspect.from_vmcore", "artifacts.list"],
            data={"report": report, "truncated": str(output.truncated).lower()},
        )


async def _live_ssh_session(
    conn: AsyncConnection, ctx: RequestContext, session_id: str
) -> tuple[str, str, UUID] | ToolResponse:
    """Resolve a `live` ssh DebugSession, returning its (project, transport_handle), or a failure.

    Gates on UUID shape, project scope, ``operator`` role, ``live`` state, and an ``ssh``
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
    return session.project, session.transport_handle, uid


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
        _project, transport_handle, _session_uid = resolved
        try:
            output = await asyncio.to_thread(
                introspector.introspect_live, transport_handle=transport_handle, helper=helper
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(session_id, exc)
        sections = {"tasks": output.tasks, "modules": output.modules, "sysinfo": output.sysinfo}
        report = {helper: sections[helper]}
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


def register(
    app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver | None = None
) -> None:
    """Register the `introspect.from_vmcore` and `introspect.run` tools on ``app``."""
    if resolver is None:
        raise RuntimeError("introspect registrar requires an injected provider resolver")

    @app.tool(
        name="introspect.from_vmcore",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def introspect_from_vmcore_tool(
        run_id: Annotated[
            str, Field(description="The Run whose captured core to introspect with drgn.")
        ],
    ) -> ToolResponse:
        """Run offline drgn introspection over a Run's captured core; returns redacted report."""
        return await with_runtime_for_run(
            pool,
            resolver,
            run_id,
            lambda runtime: introspect_from_vmcore(
                pool,
                current_context(),
                run_id=run_id,
                introspector=runtime.vmcore_introspector,
            ),
        )

    @app.tool(
        name="introspect.run",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def introspect_run_tool(
        session_id: Annotated[str, Field(description="A live ssh DebugSession to introspect.")],
        helper: Annotated[
            str,
            Field(description="In-tree drgn helper to run: tasks, modules, or sysinfo."),
        ],
    ) -> ToolResponse:
        """Run live drgn introspection over a live ssh DebugSession. Requires operator."""
        async with pool.connection() as conn:
            resolved = await _live_ssh_session(conn, current_context(), session_id)
        if isinstance(resolved, ToolResponse):
            return resolved
        _project, _transport_handle, session_uid = resolved
        async with pool.connection() as conn:
            try:
                runtime = await resolver.runtime_for_session(conn, session_uid)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(session_id, exc)
        return await introspect_run(
            pool,
            current_context(),
            session_id=session_id,
            helper=helper,
            introspector=runtime.live_introspector,
        )
