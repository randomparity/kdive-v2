"""The `vmcore.*` / `postmortem.*` MCP tools (ADR-0031).

`vmcore.fetch(system_id, method)` admits a `capture_vmcore` job on a `crashed` System
(dedup `{system_id}:capture_vmcore:{method}`). Worker-owned capture execution lives in
``kdive.planes.vmcore``; `vmcore.list` is a `redacted`-only read.
`postmortem.crash`/`.triage` are synchronous, ungated offline reads that load the Run's
`debuginfo_ref`, validate caller commands against the allowlist, run the `CrashPostmortem`
port over the captured core, and redact output before returning it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Annotated, Literal, NamedTuple
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.artifact_queries import raw_vmcore_key
from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError
from kdive.domain.models import Job, JobKind
from kdive.domain.state import SystemState
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import (
    as_uuid as _as_uuid,
)
from kdive.mcp.tools._common import (
    authorizing as job_authorizing,
)
from kdive.mcp.tools._common import (
    config_error as _config_error,
)
from kdive.mcp.tools._common import (
    job_envelope,
)
from kdive.mcp.tools.catalog import artifacts as artifacts_tools
from kdive.planes.runs_shared import existing_build_result
from kdive.providers.composition import ProviderRuntime
from kdive.providers.ports import CrashPostmortem
from kdive.security.context import RequestContext
from kdive.security.crash_commands import crash_command_rejection_reason
from kdive.security.rbac import Role, require_role
from kdive.security.redaction import Redactor

_log = logging.getLogger(__name__)

# The ported v1 crash-command allowlist (read-only crash verbs).
_CRASH_ALLOWLIST: frozenset[str] = frozenset(
    {
        "bt",
        "ps",
        "log",
        "kmem",
        "sys",
        "mod",
        "struct",
        "union",
        "p",
        "rd",
        "vtop",
        "task",
        "files",
        "vm",
        "net",
        "dev",
        "irq",
        "mach",
        "runq",
        "mount",
        "swap",
        "timer",
        "dis",
        "sym",
        "list",
        "tree",
        "search",
        "foreach",
        "help",
    }
)
_TRIAGE_COMMANDS: tuple[str, ...] = ("log", "bt")


def _system_job_envelope(job: Job, system_id: UUID) -> ToolResponse:
    return job_envelope(job, "system_id", system_id)


# --- vmcore.fetch (admission) --------------------------------------------------------------


# The core-producing methods valid for vmcore.fetch (excludes console/gdbstub).
_VMCORE_METHODS: frozenset[CaptureMethod] = frozenset(
    {CaptureMethod.HOST_DUMP, CaptureMethod.KDUMP}
)


@dataclass(frozen=True, slots=True)
class VmcoreHandlers:
    """vmcore/postmortem MCP handlers with provider seams bound at construction."""

    supported_methods: frozenset[CaptureMethod]
    crash: CrashPostmortem

    async def fetch_vmcore(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        system_id: str,
        method: str = "host_dump",
    ) -> ToolResponse:
        return await _fetch_vmcore(
            pool,
            ctx,
            system_id=system_id,
            method=method,
            supported_methods=self.supported_methods,
        )

    async def postmortem_crash(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        run_id: str,
        commands: list[str],
    ) -> ToolResponse:
        return await _postmortem_crash(
            pool, ctx, run_id=run_id, commands=commands, crash=self.crash
        )

    async def postmortem_triage(
        self, pool: AsyncConnectionPool, ctx: RequestContext, *, run_id: str
    ) -> ToolResponse:
        return await _postmortem_triage(pool, ctx, run_id=run_id, crash=self.crash)


async def _fetch_vmcore(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    method: str = "host_dump",
    supported_methods: frozenset[CaptureMethod],
) -> ToolResponse:
    """Admit a `capture_vmcore` job on a `crashed` System (operator); return the job handle."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    try:
        capture_method = CaptureMethod(method)
    except ValueError:
        return _config_error(system_id, data={"method": method, "reason": "unknown capture method"})
    if capture_method not in _VMCORE_METHODS:
        return _config_error(
            system_id,
            data={"method": method, "reason": "method does not produce a vmcore"},
        )
    if capture_method not in supported_methods:
        return _config_error(
            system_id,
            data={"method": method, "reason": "method not supported by provider"},
        )
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            require_role(ctx, system.project, Role.OPERATOR)
            if system.state is not SystemState.CRASHED:
                return _config_error(system_id, data={"current_status": system.state.value})
            job = await queue.enqueue(
                conn,
                JobKind.CAPTURE_VMCORE,
                {"system_id": system_id, "method": capture_method.value},
                job_authorizing(ctx, system.project),
                f"{system_id}:capture_vmcore:{capture_method.value}",
            )
        return _system_job_envelope(job, uid)


# --- vmcore.list ---------------------------------------------------------------------------


def _is_redacted_vmcore(object_key: str) -> bool:
    return "/vmcore-" in object_key and object_key.endswith("-redacted")


async def list_vmcores(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> ToolResponse:
    """Return the System's `redacted` vmcore artifacts in one collection envelope."""
    listed = await artifacts_tools.artifact_list_items(pool, ctx, system_id=system_id)
    items = [r for r in listed if _is_redacted_vmcore(r.refs.get("object", ""))]
    return ToolResponse.collection(
        system_id,
        "ok",
        items,
        suggested_next_actions=["artifacts.get", "postmortem.crash"],
    )


# --- postmortem.crash / .triage ------------------------------------------------------------


async def _build_id_for_run(conn: AsyncConnection, run_id: UUID) -> str | None:
    result = await existing_build_result(conn, run_id)
    return None if result is None else result.build_id


class _PostmortemTargets(NamedTuple):
    """The resolved (non-null) inputs the crash port needs to symbolize a Run's core."""

    debuginfo_ref: str
    build_id: str
    vmcore_ref: str


async def _resolve_postmortem(
    conn: AsyncConnection, ctx: RequestContext, run_id: str, commands: list[str]
) -> _PostmortemTargets | ToolResponse:
    """Resolve the debuginfo ref, build-id, and raw core key (all non-null), or a failure."""
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
    for command in commands:
        if crash_command_rejection_reason(command, _CRASH_ALLOWLIST) is not None:
            return _config_error(run_id)
    vmcore_ref = await raw_vmcore_key(conn, run.system_id)
    if vmcore_ref is None:
        return _config_error(run_id)
    return _PostmortemTargets(run.debuginfo_ref, build_id, vmcore_ref)


async def _postmortem_crash(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    commands: list[str],
    crash: CrashPostmortem,
) -> ToolResponse:
    """Run the crash command batch over the Run's captured core; redact and return (ungated)."""
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resolved = await _resolve_postmortem(conn, ctx, run_id, commands)
        if isinstance(resolved, ToolResponse):
            return resolved
        try:
            output = await asyncio.to_thread(
                crash.run_crash_postmortem,
                vmcore_ref=resolved.vmcore_ref,
                debuginfo_ref=resolved.debuginfo_ref,
                expected_build_id=resolved.build_id,
                commands=commands,
            )
        except CategorizedError as exc:
            # A provenance mismatch (configuration_error) or an unavailable crash
            # dependency (missing_dependency) becomes a typed failure, never a 500.
            return ToolResponse.failure(run_id, exc.category)
        redactor = Redactor()
        return ToolResponse.success(
            run_id,
            "succeeded",
            suggested_next_actions=["postmortem.crash", "artifacts.list"],
            data={"transcript": redactor.redact_text(output.transcript)},
        )


async def _postmortem_triage(
    pool: AsyncConnectionPool, ctx: RequestContext, *, run_id: str, crash: CrashPostmortem
) -> ToolResponse:
    """Run the fixed triage command batch and return the redacted report."""
    resp = await _postmortem_crash(
        pool, ctx, run_id=run_id, commands=list(_TRIAGE_COMMANDS), crash=crash
    )
    if resp.status == "error":
        return resp
    return resp.model_copy(
        update={"suggested_next_actions": ["postmortem.triage", "artifacts.list"]}
    )


# --- registration --------------------------------------------------------------------------


def register(
    app: FastMCP, pool: AsyncConnectionPool, *, provider_runtime: ProviderRuntime | None = None
) -> None:
    """Register the `vmcore.*` / `postmortem.*` tools on ``app``, bound to ``pool``."""
    if provider_runtime is None:
        raise RuntimeError("vmcore registrar requires an injected provider runtime")
    runtime = provider_runtime
    handlers = VmcoreHandlers(
        supported_methods=runtime.supported_capture_methods,
        crash=runtime.crash_postmortem,
    )

    @app.tool(
        name="vmcore.fetch",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def vmcore_fetch(
        system_id: Annotated[str, Field(description="The crashed System whose vmcore to capture.")],
        method: Annotated[
            Literal["host_dump", "kdump"],
            Field(description="Capture method; must be supported by the local-libvirt provider."),
        ] = "host_dump",
    ) -> ToolResponse:
        """Enqueue a capture_vmcore job on a crashed System. Requires operator."""
        return await handlers.fetch_vmcore(
            pool,
            current_context(),
            system_id=system_id,
            method=method,
        )

    @app.tool(
        name="vmcore.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def vmcore_list(
        system_id: Annotated[
            str,
            Field(description="The System whose redacted vmcore artifacts to list."),
        ],
    ) -> ToolResponse:
        """List the redacted vmcore artifacts for a System. Requires viewer."""
        return await list_vmcores(pool, current_context(), system_id=system_id)

    @app.tool(
        name="postmortem.crash",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def postmortem_crash_tool(
        run_id: Annotated[str, Field(description="The Run whose captured core to analyze.")],
        commands: Annotated[
            list[str],
            Field(description="Crash commands to run (allowlisted read-only verbs)."),
        ],
    ) -> ToolResponse:
        """Run a crash command batch over a Run's captured core; returns redacted output."""
        return await handlers.postmortem_crash(
            pool, current_context(), run_id=run_id, commands=commands
        )

    @app.tool(
        name="postmortem.triage",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def postmortem_triage_tool(
        run_id: Annotated[str, Field(description="The Run whose captured core to triage.")],
    ) -> ToolResponse:
        """Run the fixed triage commands (log+bt) over a Run's captured core; redacted report."""
        return await handlers.postmortem_triage(pool, current_context(), run_id=run_id)
