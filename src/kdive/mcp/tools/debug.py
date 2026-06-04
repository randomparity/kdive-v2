"""The `debug.*` session-lifecycle MCP tools — the Connect plane surface (ADR-0032).

`debug.start_session(run_id, "gdbstub")` opens a single-attach gdbstub transport to the
Run's `ready` System and inserts a `debug_sessions` row `attach -> live` carrying the
transport handle + an initial heartbeat; `debug.end_session(session_id)` drives a live/attach
session `-> detached`. Both are **synchronous** (no JobKind): opening the transport is a
bounded RSP probe, not a long-running provider op.

Single-attach is per **System** (per gdbstub endpoint), joined through `runs.system_id` —
two Runs on one System share the one stub, so a second attach is `transport_conflict`. The
RSP probe runs **outside** the per-System advisory lock (it is multi-second network IO);
the conflict + System-ready checks are re-evaluated authoritatively under the lock before the
insert, and a lost race closes the just-opened transport (ADR-0032 §6a). The
`force_crash`/reboot `live -> detached` path is the control plane's (#23, `control.py`
`_detach_sessions`); this module owns only the agent-initiated start/end.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import LiteralString
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import DEBUG_SESSIONS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import DebugSession, Run, System
from kdive.domain.state import DebugSessionState, RunState, SystemState
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.debug_ops import DebugEngineRuntime, register_debug_ops
from kdive.providers.interfaces import SystemHandle, TransportHandle
from kdive.providers.local_libvirt.connect import Connector, LocalLibvirtConnect
from kdive.providers.local_libvirt.debug_gdbmi import GdbMiEngine, default_attach_seam
from kdive.security import audit
from kdive.security.rbac import Role, require_role

_GDBSTUB = "gdbstub"
# An attach failure maps these provider categories onto the response envelope. A
# MISSING_DEPENDENCY (no live_vm host / unresolvable endpoint) surfaces as an attach failure:
# the agent cannot attach either way.
_ATTACH_FAILURE = frozenset({ErrorCategory.DEBUG_ATTACH_FAILURE, ErrorCategory.TRANSPORT_FAILURE})

# A live/attach session occupies the System's single gdbstub endpoint (single-attach).
_OCCUPIED_SQL: LiteralString = (
    "SELECT 1 FROM debug_sessions s "
    "JOIN runs r ON r.id = s.run_id "
    "WHERE r.system_id = %s AND s.state IN ('attach', 'live') LIMIT 1"
)


def _config_error(object_id: str, *, data: dict[str, str] | None = None) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


async def _system_for_run(conn: AsyncConnection, run: Run) -> System | None:
    return await SYSTEMS.get(conn, run.system_id)


async def _has_succeeded_boot(conn: AsyncConnection, run_id: UUID) -> bool:
    """Report whether a succeeded ``boot`` step exists for ``run_id`` (the booted signal)."""
    query: LiteralString = (
        "SELECT 1 FROM run_steps WHERE run_id = %s AND step = 'boot' AND state = 'succeeded'"
    )
    async with conn.cursor() as cur:
        await cur.execute(query, (run_id,))
        return await cur.fetchone() is not None


async def _system_occupied(conn: AsyncConnection, system_id: UUID) -> bool:
    """Report whether the System already has an ``attach``/``live`` session (single-attach)."""
    async with conn.cursor() as cur:
        await cur.execute(_OCCUPIED_SQL, (system_id,))
        return await cur.fetchone() is not None


def _open_transport(connector: Connector, system: System) -> TransportHandle | ToolResponse:
    """Open the gdbstub transport outside any lock; map a provider failure to an envelope."""
    handle_name = system.domain_name or str(system.id)
    try:
        return connector.open_transport(SystemHandle(handle_name), _GDBSTUB)
    except CategorizedError as exc:
        category = exc.category if exc.category in _ATTACH_FAILURE else _mapped(exc.category)
        return ToolResponse.failure(str(system.id), category)


def _mapped(category: ErrorCategory) -> ErrorCategory:
    """Map a non-attach provider category onto the response taxonomy (MISSING_DEPENDENCY)."""
    if category is ErrorCategory.MISSING_DEPENDENCY:
        return ErrorCategory.DEBUG_ATTACH_FAILURE
    return category


async def start_session(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    transport: str = _GDBSTUB,
    connector: Connector,
) -> ToolResponse:
    """Open a single-attach gdbstub transport and insert a `live` DebugSession (operator)."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    if transport != _GDBSTUB:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.OPERATOR)
            guard = await _attach_preconditions(conn, run)
            if isinstance(guard, ToolResponse):
                return guard
            system = guard
            opened = _open_transport(connector, system)
            if isinstance(opened, ToolResponse):
                return opened
            return await _insert_session_locked(conn, ctx, run, system, opened, connector)


async def _attach_preconditions(conn: AsyncConnection, run: Run) -> System | ToolResponse:
    """Lockless pre-checks: Run booted, System present + `ready`, endpoint free.

    Returns the System on success, or a failure envelope. These are advisory fast-fails;
    `_insert_session_locked` re-checks conflict + ready authoritatively under the lock.
    """
    if run.state is not RunState.SUCCEEDED:
        return _config_error(str(run.id), data={"current_status": run.state.value})
    if not await _has_succeeded_boot(conn, run.id):
        return _config_error(str(run.id), data={"reason": "boot_first"})
    system = await _system_for_run(conn, run)
    if system is None:
        return _config_error(str(run.id))
    if system.state is not SystemState.READY:
        return _config_error(str(run.id), data={"current_status": system.state.value})
    if await _system_occupied(conn, system.id):
        return ToolResponse.failure(str(run.id), ErrorCategory.TRANSPORT_CONFLICT)
    return system


async def _insert_session_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    system: System,
    handle: TransportHandle,
    connector: Connector,
) -> ToolResponse:
    """Re-check conflict + ready under the per-System lock, then insert + drive `-> live`.

    A lost race (System crashed, or another attach committed first) closes the just-opened
    transport and returns the categorized error — no `live` row escapes the lock.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system.id):
        current = await SYSTEMS.get(conn, system.id)
        if current is None or current.state is not SystemState.READY:
            connector.close_transport(handle)
            status = current.state.value if current else "torn_down"
            return _config_error(str(run.id), data={"current_status": status})
        if await _system_occupied(conn, system.id):
            connector.close_transport(handle)
            return ToolResponse.failure(str(run.id), ErrorCategory.TRANSPORT_CONFLICT)
        now = datetime.now(UTC)  # placeholder; the DB owns created_at/updated_at
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=uuid4(),
                created_at=now,
                updated_at=now,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=run.project,
                run_id=run.id,
                state=DebugSessionState.ATTACH,
                transport=_GDBSTUB,
                transport_handle=str(handle),
                worker_heartbeat_at=now,
            ),
        )
        await audit.record(
            conn,
            ctx,
            tool="debug.start_session",
            object_kind="debug_sessions",
            object_id=session.id,
            transition="->attach",
            args={"run_id": str(run.id)},
            project=run.project,
        )
        await DEBUG_SESSIONS.update_state(conn, session.id, DebugSessionState.LIVE)
        await audit.record(
            conn,
            ctx,
            tool="debug.start_session",
            object_kind="debug_sessions",
            object_id=session.id,
            transition="attach->live",
            args={"run_id": str(run.id)},
            project=run.project,
        )
    return ToolResponse.success(
        str(session.id),
        "live",
        suggested_next_actions=["debug.end_session"],
        data={"project": run.project},
    )


async def _resolve_session_system(
    conn: AsyncConnection, session_id: UUID
) -> tuple[DebugSession, UUID] | None:
    """Resolve a session and its System id via the `debug_sessions -> runs` join."""
    session = await DEBUG_SESSIONS.get(conn, session_id)
    if session is None:
        return None
    run = await RUNS.get(conn, session.run_id)
    if run is None:
        return None
    return session, run.system_id


async def end_session(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    session_id: str,
    *,
    connector: Connector,
    runtime: DebugEngineRuntime | None = None,
) -> ToolResponse:
    """Drive a live/attach DebugSession `-> detached` (idempotent on detached; operator).

    Also reaps the lazy gdb-MI engine (ADR-0034 §4d): under the per-session lock it exits the
    gdb subprocess and drops the registry entry, so an ended session never strands a subprocess
    or holds the single-attach stub. Reaping a session that never ran a Debug-plane op is a
    no-op.
    """
    uid = _as_uuid(session_id)
    if uid is None:
        return _config_error(session_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            session = await DEBUG_SESSIONS.get(conn, uid)
            if session is None or session.project not in ctx.projects:
                return _config_error(session_id)
            require_role(ctx, session.project, Role.OPERATOR)
            resolved = await _resolve_session_system(conn, uid)
            if resolved is None:
                return _config_error(session_id)
            _, system_id = resolved
            envelope = await _detach_locked(conn, ctx, uid, system_id, connector)
        if runtime is not None:
            async with runtime.lock_for(session_id):
                runtime.reap(session_id)
        return envelope


async def _detach_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    session_id: UUID,
    system_id: UUID,
    connector: Connector,
) -> ToolResponse:
    select_q: LiteralString = (
        "SELECT state, transport_handle, project FROM debug_sessions WHERE id = %s FOR UPDATE"
    )
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(select_q, (session_id,))
            row = await cur.fetchone()
        if row is None:
            return _config_error(str(session_id))
        if row["state"] == DebugSessionState.DETACHED.value:
            return _detached_envelope(session_id, row["project"])
        _close(connector, row["transport_handle"])
        await DEBUG_SESSIONS.update_state(conn, session_id, DebugSessionState.DETACHED)
        await audit.record(
            conn,
            ctx,
            tool="debug.end_session",
            object_kind="debug_sessions",
            object_id=session_id,
            transition=f"{row['state']}->detached",
            args={"session_id": str(session_id)},
            project=row["project"],
        )
    return _detached_envelope(session_id, row["project"])


def _close(connector: Connector, handle: str | None) -> None:
    """Close the transport best-effort; a missing/failing close never blocks the detach."""
    if handle is None:
        return
    with contextlib.suppress(CategorizedError):
        connector.close_transport(TransportHandle(handle))


def _detached_envelope(session_id: UUID, project: str) -> ToolResponse:
    return ToolResponse.success(
        str(session_id), "detached", suggested_next_actions=[], data={"project": project}
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `debug.*` tools on ``app``, bound to ``pool``.

    The `Connector` is built once via `LocalLibvirtConnect.from_env()` (no libvirt connection
    at registration — the resolver/prober are lazy `live_vm` seams). The Debug-plane gdb-MI
    tier (ADR-0034) shares one process-scoped :class:`DebugEngineRuntime` (registry +
    per-session locks + the `live_vm`-gated attach seam); its seven tools register here too, so
    `app.py` is untouched. `end_session` reaps the lazy engine via the shared runtime.
    """
    connector: Connector = LocalLibvirtConnect.from_env()
    runtime = DebugEngineRuntime(engine=GdbMiEngine(), attach=default_attach_seam)

    @app.tool(name="debug.start_session")
    async def debug_start_session(run_id: str, transport: str = _GDBSTUB) -> ToolResponse:
        return await start_session(
            pool, current_context(), run_id=run_id, transport=transport, connector=connector
        )

    @app.tool(name="debug.end_session")
    async def debug_end_session(session_id: str) -> ToolResponse:
        return await end_session(
            pool, current_context(), session_id, connector=connector, runtime=runtime
        )

    register_debug_ops(app, pool, runtime)
