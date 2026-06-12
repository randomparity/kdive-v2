"""The `introspect.from_vmcore` MCP tool: offline drgn introspection of a captured vmcore.

`introspect.from_vmcore(run_id)` is a synchronous offline viewer read (ADR-0033). It resolves
the Run's `debuginfo_ref` (the build-plane `vmlinux`), the build plane's recorded `build_id`
(provenance), and the Run's System's captured raw `vmcore` key through the shared
`mcp.tools._vmcore_targets` helper. It then runs the `VmcoreIntrospector` port and returns the
**already-redacted** report (the port is the single redaction boundary, ADR-0033 §6) as
structured data in `data["report"]`.

Real drgn is an operator-provided live-host prerequisite. Normal service startup leaves the
drgn-backed seams disabled; the live runner injects them only on hosts prepared for
``live_vm`` debugging.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, NamedTuple, cast
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ResponseData, ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run
from kdive.mcp.tools._vmcore_targets import resolve_run_vmcore_target
from kdive.mcp.tools.debug.session_context import resolve_debug_session_context
from kdive.providers.ports import LiveIntrospector, VmcoreIntrospector
from kdive.providers.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext

# The fixed live-helper set (ADR-0033 §2 / ADR-0039 §3): the same three in-tree helpers as the
# offline path. There is no caller-supplied drgn script — an unknown helper is rejected.
_LIVE_HELPERS = frozenset({"tasks", "modules", "sysinfo"})
_DRGN_LIVE = "drgn-live"


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
    Off a prepared live host, the provider seam reports ``missing_dependency`` instead of
    importing drgn.
    """
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            try:
                resolved = await resolve_run_vmcore_target(conn, ctx, run_id)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(run_id, exc)
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
            data=cast(
                ResponseData,
                {"report": report, "truncated": str(output.truncated).lower()},
            ),
        )


class LiveDrgnSession(NamedTuple):
    """The resolved inputs needed to run live drgn introspection."""

    project: str
    transport_handle: str
    session_id: UUID


async def resolve_live_drgn_session(
    conn: AsyncConnection, ctx: RequestContext, session_id: str
) -> LiveDrgnSession:
    """Resolve a `live` drgn-live DebugSession to the domain inputs required by the port.

    Gates on UUID shape, project scope, ``operator`` role, ``live`` state, and the
    ``drgn-live`` transport (live introspection rides drgn-live, not gdbstub; ADR-0039 §4 /
    ADR-0085). The provider realizes drgn-live over SSH (local) or the guest agent (remote);
    core treats the resolved ``transport_handle`` as opaque.
    """
    resolved = await resolve_debug_session_context(
        conn,
        ctx,
        session_id,
        required_transport=_DRGN_LIVE,
        require_live=True,
    )
    if isinstance(resolved, ToolResponse) or resolved.transport_handle is None:
        raise _session_config_error()
    return LiveDrgnSession(resolved.project, resolved.transport_handle, resolved.session_id)


def _session_config_error() -> CategorizedError:
    return CategorizedError(
        "debug session does not resolve to a live drgn-live session",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


async def introspect_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    session_id: str,
    helper: str,
    introspector: LiveIntrospector,
) -> ToolResponse:
    """Run live drgn introspection over a `live` drgn-live DebugSession; return a redacted report.

    Requires a `live` drgn-live DebugSession (operator). The ``helper`` must be one of the fixed
    in-tree helpers — there is no caller-supplied drgn script. The port is the single redaction
    boundary, so the returned report is already masked; the raw drgn transcript is ``sensitive``
    and is never returned (the response only advertises that, ADR-0039 §2/§3).
    Off a prepared live host, the provider seam reports ``missing_dependency`` instead of
    importing drgn.
    """
    if helper not in _LIVE_HELPERS:
        return _config_error(session_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            try:
                resolved = await resolve_live_drgn_session(conn, ctx, session_id)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(session_id, exc)
        return await _introspect_live_session(
            session_id,
            resolved=resolved,
            helper=helper,
            introspector=introspector,
        )


async def _introspect_live_session(
    response_id: str,
    *,
    resolved: LiveDrgnSession,
    helper: str,
    introspector: LiveIntrospector,
) -> ToolResponse:
    if helper not in _LIVE_HELPERS:
        return _config_error(response_id)
    try:
        output = await asyncio.to_thread(
            introspector.introspect_live,
            transport_handle=resolved.transport_handle,
            helper=helper,
        )
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(response_id, exc)
    sections = {"tasks": output.tasks, "modules": output.modules, "sysinfo": output.sysinfo}
    report = {helper: sections[helper]}
    return ToolResponse.success(
        response_id,
        "succeeded",
        suggested_next_actions=["introspect.run", "debug.end_session"],
        data=cast(
            ResponseData,
            {
                "report": report,
                "truncated": str(output.truncated).lower(),
                "transcript_sensitivity": "sensitive",
            },
        ),
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
            str,
            Field(
                description=(
                    "The Run whose captured core to introspect with operator-provided drgn."
                )
            ),
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
        session_id: Annotated[str, Field(description="A live drgn-live DebugSession.")],
        helper: Annotated[
            str,
            Field(
                description=(
                    "In-tree drgn helper to run with operator-provided drgn: tasks, modules, "
                    "or sysinfo."
                )
            ),
        ],
    ) -> ToolResponse:
        """Run live drgn introspection over a live drgn-live DebugSession. Requires operator."""
        async with pool.connection() as conn:
            try:
                resolved = await resolve_live_drgn_session(conn, current_context(), session_id)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(session_id, exc)
        async with pool.connection() as conn:
            try:
                runtime = await resolver.runtime_for_session(conn, resolved.session_id)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(session_id, exc)
        return await _introspect_live_session(
            session_id,
            resolved=resolved,
            helper=helper,
            introspector=runtime.live_introspector,
        )
