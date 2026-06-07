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
from typing import Annotated, Literal, LiteralString, NamedTuple
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.capture import LOCAL_LIBVIRT_SUPPORTED, CaptureMethod
from kdive.domain.errors import CategorizedError
from kdive.domain.models import Job, JobKind
from kdive.domain.state import SystemState
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools import artifacts as artifacts_tools
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
from kdive.planes.vmcore import RAW_KEY_LIKE as _RAW_KEY_LIKE
from kdive.planes.vmcore import RAW_KEY_SQL as _RAW_KEY_SQL
from kdive.planes.vmcore import REDACTED_LIKE as _REDACTED_LIKE
from kdive.providers.composition import (
    ProviderRuntime,
    crash_command_rejection_reason,
    crash_postmortem_from_env,
)
from kdive.providers.ports import CrashPostmortem
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

_BUILD_STEP_SQL: LiteralString = "SELECT result FROM run_steps WHERE run_id = %s AND step = 'build'"


def _system_job_envelope(job: Job, system_id: UUID) -> ToolResponse:
    return job_envelope(job, "system_id", system_id)


# --- vmcore.fetch (admission) --------------------------------------------------------------


# The core-producing methods valid for vmcore.fetch (excludes console/gdbstub).
_VMCORE_METHODS: frozenset[CaptureMethod] = frozenset(
    {CaptureMethod.HOST_DUMP, CaptureMethod.KDUMP}
)


async def fetch_vmcore(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    method: str = "host_dump",
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
    if capture_method not in LOCAL_LIBVIRT_SUPPORTED:
        return _config_error(
            system_id,
            data={"method": method, "reason": "method not supported by local-libvirt provider"},
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
) -> list[ToolResponse]:
    """Return the System's `redacted` vmcore artifacts (`artifacts.list` for the vmcore rows)."""
    listed = await artifacts_tools.artifacts_list(pool, ctx, system_id=system_id)
    return [r for r in listed if _is_redacted_vmcore(r.refs.get("object", ""))]


# --- postmortem.crash / .triage ------------------------------------------------------------


async def _build_id_for_run(conn: AsyncConnection, run_id: UUID) -> str | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_BUILD_STEP_SQL, (run_id,))
        row = await cur.fetchone()
    if row is None or not isinstance(row["result"], dict):
        return None
    build_id = row["result"].get("build_id")
    return build_id if isinstance(build_id, str) and build_id else None


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
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RAW_KEY_SQL, (run.system_id, _RAW_KEY_LIKE, _REDACTED_LIKE))
        row = await cur.fetchone()
    if row is None:
        return _config_error(run_id)
    return _PostmortemTargets(run.debuginfo_ref, build_id, str(row["object_key"]))


async def postmortem_crash(
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
                crash.run,
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


async def postmortem_triage(
    pool: AsyncConnectionPool, ctx: RequestContext, *, run_id: str, crash: CrashPostmortem
) -> ToolResponse:
    """Run the fixed triage command batch and return the redacted report."""
    resp = await postmortem_crash(
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
    crash = provider_runtime.crash_postmortem() if provider_runtime else crash_postmortem_from_env()

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
        return await fetch_vmcore(pool, current_context(), system_id=system_id, method=method)

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
    ) -> list[ToolResponse]:
        """List the redacted vmcore artifacts for a System. Requires project membership."""
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
        return await postmortem_crash(
            pool, current_context(), run_id=run_id, commands=commands, crash=crash
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
        return await postmortem_triage(pool, current_context(), run_id=run_id, crash=crash)
