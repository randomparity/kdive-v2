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
from typing import Annotated, Literal

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import SYSTEMS
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError
from kdive.domain.models import JobKind
from kdive.domain.state import SystemState
from kdive.jobs import queue
from kdive.jobs.payloads import CaptureVmcorePayload
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
from kdive.mcp.tools._vmcore_targets import resolve_run_vmcore_target
from kdive.providers.ports import CrashPostmortem
from kdive.providers.runtime import ProviderRuntime
from kdive.security.artifacts.crash_commands import crash_command_rejection_reason
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.security.secrets.redaction import Redactor
from kdive.services.artifact_listing import RedactedArtifact, list_redacted_system_artifacts

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
                CaptureVmcorePayload(system_id=system_id, method=capture_method),
                job_authorizing(ctx, system.project),
                f"{system_id}:capture_vmcore:{capture_method.value}",
            )
        return job_envelope(job, "system_id", uid)


# --- vmcore.list ---------------------------------------------------------------------------


def _is_redacted_vmcore(object_key: str) -> bool:
    return "/vmcore-" in object_key and object_key.endswith("-redacted")


async def list_vmcores(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> ToolResponse:
    """Return the System's `redacted` vmcore artifacts in one collection envelope."""
    listed = await list_redacted_system_artifacts(pool, ctx, system_id=system_id)
    items = [_vmcore_item(row) for row in listed if _is_redacted_vmcore(row.object_key)]
    return ToolResponse.collection(
        system_id,
        "ok",
        items,
        suggested_next_actions=["artifacts.get", "postmortem.crash"],
    )


def _vmcore_item(artifact: RedactedArtifact) -> ToolResponse:
    return ToolResponse.success(
        artifact.id,
        "available",
        suggested_next_actions=["artifacts.get"],
        refs={"object": artifact.object_key},
    )


# --- postmortem.crash / .triage ------------------------------------------------------------


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
        for command in commands:
            if crash_command_rejection_reason(command, _CRASH_ALLOWLIST) is not None:
                return _config_error(run_id)
        async with pool.connection() as conn:
            resolved = await resolve_run_vmcore_target(conn, ctx, run_id)
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
