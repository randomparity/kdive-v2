"""The `vmcore.*` / `postmortem.*` MCP tools + the capture job handler (ADR-0031).

`vmcore.fetch(system_id)` admits a `capture_vmcore` job on a `crashed` System
(dedup `{system_id}:capture_vmcore`); `capture_handler` waits for kdump under the per-System
advisory lock, stores the raw `sensitive` core + a `redacted` derivative, and inserts both
`artifacts` rows (skipping re-capture if a `vmcore` row already exists). `vmcore.list` is a
`redacted`-only read. `postmortem.crash`/`.triage` are synchronous, ungated offline reads that
load the Run's `debuginfo_ref`, validate caller commands against the allowlist, run the
`CrashPostmortem` port over the captured core, and redact output before returning it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, LiteralString, NamedTuple
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, System
from kdive.domain.state import SystemState
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import artifacts as artifacts_tools
from kdive.providers.local_libvirt.retrieve import (
    CrashPostmortem,
    LocalLibvirtRetrieve,
    Retriever,
    crash_command_rejection_reason,
)
from kdive.security import audit
from kdive.security.rbac import Role, require_role
from kdive.security.redaction import Redactor
from kdive.store.objectstore import register_artifact_row

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

_RAW_KEY_SQL: LiteralString = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND object_key LIKE %s"
)
_BUILD_STEP_SQL: LiteralString = "SELECT result FROM run_steps WHERE run_id = %s AND step = 'build'"


def _config_error(object_id: str, *, data: dict[str, str] | None = None) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _authorizing(ctx: RequestContext, project: str) -> dict[str, Any]:
    return {"principal": ctx.principal, "agent_session": ctx.agent_session, "project": project}


def _ctx_from_job(job: Job, project: str) -> RequestContext:
    auth = job.authorizing
    agent_session: str | None = auth.get("agent_session")
    return RequestContext(
        principal=str(auth["principal"]),
        agent_session=agent_session,
        projects=(project,),
        roles={},
    )


def _system_job_envelope(job: Job, system_id: UUID) -> ToolResponse:
    base = ToolResponse.from_job(job)
    return base.model_copy(update={"data": {**base.data, "system_id": str(system_id)}})


# --- vmcore.fetch (admission) --------------------------------------------------------------


async def fetch_vmcore(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> ToolResponse:
    """Admit a `capture_vmcore` job on a `crashed` System (operator); return the job handle."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
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
                {"system_id": system_id},
                _authorizing(ctx, system.project),
                f"{system_id}:capture_vmcore",
            )
        return _system_job_envelope(job, uid)


# --- capture handler -----------------------------------------------------------------------


async def _existing_raw_key(conn: AsyncConnection, system_id: UUID) -> str | None:
    """Return the System's raw `vmcore` object key, or ``None`` (the execution ledger)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RAW_KEY_SQL, (system_id, "%/vmcore"))
        row = await cur.fetchone()
    return None if row is None else str(row["object_key"])


async def _precheck_system(conn: AsyncConnection, system_id: UUID) -> System | str:
    """Under the per-System lock, return an existing raw key (str) or the System to capture."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "capture target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        existing = await _existing_raw_key(conn, system_id)
        return existing if existing is not None else system


async def _finalize_capture(conn: AsyncConnection, job: Job, system: System, output: Any) -> str:
    """Insert both artifact rows + audit under the per-System lock; tolerate a concurrent win."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system.id):
        existing = await _existing_raw_key(conn, system.id)
        if existing is not None:
            return existing
        await ARTIFACTS.insert(
            conn, register_artifact_row(output.raw, owner_kind="systems", owner_id=system.id)
        )
        await ARTIFACTS.insert(
            conn, register_artifact_row(output.redacted, owner_kind="systems", owner_id=system.id)
        )
        await audit.record(
            conn,
            _ctx_from_job(job, system.project),
            tool="vmcore.fetch",
            object_kind="systems",
            object_id=system.id,
            transition="capture_vmcore",
            args={"system_id": str(system.id)},
            project=system.project,
        )
    return str(output.raw.key)


async def capture_handler(conn: AsyncConnection, job: Job, retriever: Retriever) -> str | None:
    """Capture the System's vmcore and store the raw + redacted rows (ADR-0031 §2/§4).

    Three phases (mirroring `build_handler`): a lock-held pre-check that returns the existing
    raw key on a re-dispatch; the slow `capture` seam with **no transaction held**; a lock-held
    finalize that inserts both `artifacts` rows. A capture `CategorizedError` (e.g.
    `readiness_failure` for no core) propagates so the worker dead-letters the job.
    """
    system_id = UUID(job.payload["system_id"])
    precheck = await _precheck_system(conn, system_id)
    if isinstance(precheck, str):
        return precheck
    output = await asyncio.to_thread(retriever.capture, system_id)
    return await _finalize_capture(conn, job, precheck, output)


# --- vmcore.list ---------------------------------------------------------------------------


async def list_vmcores(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> list[ToolResponse]:
    """Return the System's `redacted` vmcore artifacts (`artifacts.list` for the vmcore rows)."""
    listed = await artifacts_tools.artifacts_list(pool, ctx, system_id=system_id)
    return [r for r in listed if r.refs.get("object", "").endswith("/vmcore-redacted")]


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
    if run.debuginfo_ref is None:
        return _config_error(run_id)
    build_id = await _build_id_for_run(conn, uid)
    if build_id is None:
        return _config_error(run_id)
    for command in commands:
        if crash_command_rejection_reason(command, _CRASH_ALLOWLIST) is not None:
            return _config_error(run_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RAW_KEY_SQL, (run.system_id, "%/vmcore"))
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


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `vmcore.*` / `postmortem.*` tools on ``app``, bound to ``pool``."""
    crash = LocalLibvirtRetrieve.from_env()

    @app.tool(name="vmcore.fetch")
    async def vmcore_fetch(system_id: str) -> ToolResponse:
        return await fetch_vmcore(pool, current_context(), system_id=system_id)

    @app.tool(name="vmcore.list")
    async def vmcore_list(system_id: str) -> list[ToolResponse]:
        return await list_vmcores(pool, current_context(), system_id=system_id)

    @app.tool(name="postmortem.crash")
    async def postmortem_crash_tool(run_id: str, commands: list[str]) -> ToolResponse:
        return await postmortem_crash(
            pool, current_context(), run_id=run_id, commands=commands, crash=crash
        )

    @app.tool(name="postmortem.triage")
    async def postmortem_triage_tool(run_id: str) -> ToolResponse:
        return await postmortem_triage(pool, current_context(), run_id=run_id, crash=crash)


def register_handlers(registry: HandlerRegistry, *, retriever: Retriever | None = None) -> None:
    """Bind the `capture_vmcore` job handler; build the retriever lazily from env."""
    active = retriever or LocalLibvirtRetrieve.from_env()

    async def _capture(conn: AsyncConnection, job: Job) -> str | None:
        return await capture_handler(conn, job, active)

    registry.register(JobKind.CAPTURE_VMCORE, _capture)
