"""The `control.*` MCP tools and the power/force_crash job handlers (ADR-0027).

`control.power` (ungated, operator) and `control.force_crash` (three-check gated, admin)
admit synchronously and enqueue a durable job; the handlers drive the domain via the
injected `Controller` under the per-System advisory lock. `power` moves no System state
(a domain restart is not a reprovision); `force_crash` drives System ``ready -> crashed``
and every non-terminal DebugSession of the System ``-> detached`` (joined through ``runs``).

`power` uses a per-call-unique ``dedup_key`` (``{system_id}:power:{action}:{uuid4}``) so a
repeated power op is always a fresh job; `force_crash` uses a stable
``{system_id}:force_crash`` key (once-per-System: one System per Allocation, no reprovision,
``ready -> crashed`` is one-way). Handlers reconstruct a RequestContext from the job's
authorizing tuple to audit (ADR-0025 §9).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, System
from kdive.domain.state import SystemState
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.control import Controller, LocalLibvirtControl, PowerAction
from kdive.providers.local_libvirt.provisioning import domain_name_for
from kdive.security import audit
from kdive.security.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.rbac import Role, require_role

_log = logging.getLogger(__name__)

# Systems that have a started libvirt domain (so a power op has something to act on).
_STARTED_SYSTEM = frozenset({SystemState.READY, SystemState.CRASHED})
# Terminal Systems: a force_crash that finds one returns without crashing (teardown won).
_TERMINAL_SYSTEM = frozenset({SystemState.TORN_DOWN, SystemState.FAILED})
_FORCE_CRASH = "force_crash"


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
    """Reconstruct an attribution context from a job's authorizing tuple (ADR-0025 §9)."""
    auth = job.authorizing
    agent_session: str | None = auth.get("agent_session")
    return RequestContext(
        principal=str(auth["principal"]),
        agent_session=agent_session,
        projects=(project,),
        roles={},
    )


def _system_job_envelope(job: Job, system_id: UUID) -> ToolResponse:
    """A job-handle envelope (like `from_job`) carrying the System id in ``data``."""
    base = ToolResponse.from_job(job)
    return base.model_copy(update={"data": {**base.data, "system_id": str(system_id)}})


def _domain_name(system: System) -> str:
    return system.domain_name or domain_name_for(system.id)


async def power_system(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str, action: str
) -> ToolResponse:
    """Admit a power op on a started System and enqueue a `power` job (operator, ungated)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    try:
        power_action = PowerAction(action)
    except ValueError:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            require_role(ctx, system.project, Role.OPERATOR)
            if system.state not in _STARTED_SYSTEM:
                return _config_error(system_id, data={"current_status": system.state.value})
            job = await queue.enqueue(
                conn,
                JobKind.POWER,
                {"system_id": system_id, "action": power_action.value},
                _authorizing(ctx, system.project),
                f"{system_id}:power:{power_action.value}:{uuid4()}",
            )
        return _system_job_envelope(job, uid)


async def power_handler(conn: AsyncConnection, job: Job, control: Controller) -> str | None:
    """Drive the domain's power; audit `power:{action}`; move no System state (ADR-0027 §3)."""
    system_id = UUID(job.payload["system_id"])
    action = PowerAction(job.payload["action"])
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "power target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        control.power(_domain_name(system), action)
        await audit.record(
            conn,
            _ctx_from_job(job, system.project),
            tool="control.power",
            object_kind="systems",
            object_id=system_id,
            transition=f"power:{action.value}",
            args={"system_id": str(system_id), "action": action.value},
            project=system.project,
        )
    return str(system_id)


def _opt_in(system: System) -> bool:
    """Resolve the gate's profile opt-in factor from the System's provisioning profile."""
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    return _FORCE_CRASH in profile.provider.local_libvirt.destructive_ops


async def force_crash_system(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> ToolResponse:
    """Gate, admit, and enqueue a `force_crash` job for a `ready` System (admin + gate).

    The in-project check precedes the gate, so the denial audit's ``project`` is always in
    ``ctx.projects`` and ``audit.record`` cannot itself raise (ADR-0027 ordering invariant).
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            allocation = await ALLOCATIONS.get(conn, system.allocation_id)
            if allocation is None or allocation.project not in ctx.projects:
                return _config_error(system_id)
            op = DestructiveOp(kind=_FORCE_CRASH, profile_opt_in=_opt_in(system))
            try:
                assert_destructive_allowed(ctx, allocation, op)
            except DestructiveOpDenied as denied:
                async with conn.transaction():
                    await audit.record(
                        conn,
                        ctx,
                        tool="control.force_crash",
                        object_kind="systems",
                        object_id=uid,
                        transition="force_crash:denied",
                        args={"system_id": system_id, "missing": denied.missing},
                        project=system.project,
                    )
                return ToolResponse.failure(system_id, ErrorCategory.AUTHORIZATION_DENIED)
            if system.state is not SystemState.READY:
                return _config_error(system_id, data={"current_status": system.state.value})
            job = await queue.enqueue(
                conn,
                JobKind.FORCE_CRASH,
                {"system_id": system_id},
                _authorizing(ctx, system.project),
                f"{system_id}:force_crash",
            )
        return _system_job_envelope(job, uid)


async def force_crash_handler(conn: AsyncConnection, job: Job, control: Controller) -> str | None:
    """Crash the guest and drive System ready->crashed + DebugSession live->detached.

    The System is read and mutated under the per-System advisory lock, which serializes every
    System mutation in this codebase (provision/teardown all hold ``LockScope.SYSTEM``), so the
    admission ``ready`` check being advisory is safe: a concurrent teardown that drove the
    System terminal is observed here under the lock. A terminal System skips the NMI and any
    transition; an already-``crashed`` System re-attempts the idempotent NMI but makes no
    transition. The NMI runs before the transition so a provider failure leaves the System
    untouched and the job retryable.
    """
    system_id = UUID(job.payload["system_id"])
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "force_crash target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        if system.state in _TERMINAL_SYSTEM:
            return str(system_id)  # teardown/failure won the race; nothing to crash
        control.force_crash(_domain_name(system))
        if system.state is SystemState.READY:
            await SYSTEMS.update_state(conn, system_id, SystemState.CRASHED)
            await audit.record(
                conn,
                _ctx_from_job(job, system.project),
                tool="control.force_crash",
                object_kind="systems",
                object_id=system_id,
                transition="ready->crashed",
                args={"system_id": str(system_id)},
                project=system.project,
            )
        await _detach_sessions(conn, job, system)
    return str(system_id)


async def _detach_sessions(conn: AsyncConnection, job: Job, system: System) -> None:
    """Drive every non-terminal DebugSession of ``system`` to detached (join through runs).

    A CTE captures each session's pre-update state so the audit transition reads
    ``{old}->detached`` (plain ``RETURNING state`` would yield the already-written
    ``detached``).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "WITH targets AS ("
            "    SELECT id, state FROM debug_sessions "
            "    WHERE state IN ('attach', 'live') "
            "      AND run_id IN (SELECT id FROM runs WHERE system_id = %s) "
            "    FOR UPDATE"
            ") "
            "UPDATE debug_sessions s SET state = 'detached' "
            "FROM targets t WHERE s.id = t.id "
            "RETURNING s.id, t.state",
            (system.id,),
        )
        rows = await cur.fetchall()
    for session_id, old_state in rows:
        await audit.record(
            conn,
            _ctx_from_job(job, system.project),
            tool="control.force_crash",
            object_kind="debug_sessions",
            object_id=session_id,
            transition=f"{old_state}->detached",
            args={"system_id": str(system.id)},
            project=system.project,
        )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `control.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="control.power")
    async def control_power(system_id: str, action: str) -> ToolResponse:
        return await power_system(pool, current_context(), system_id=system_id, action=action)

    @app.tool(name="control.force_crash")
    async def control_force_crash(system_id: str) -> ToolResponse:
        return await force_crash_system(pool, current_context(), system_id=system_id)


def register_handlers(registry: HandlerRegistry, *, control: Controller | None = None) -> None:
    """Bind the `power`/`force_crash` job handlers; build the provider lazily from env.

    Building the provider does not open a libvirt connection (the ``connect`` lambda is lazy),
    so the worker boots without a reachable host; the first job is the first connection.
    """
    ctrl = control or LocalLibvirtControl.from_env()

    async def _power(conn: AsyncConnection, job: Job) -> str | None:
        return await power_handler(conn, job, ctrl)

    async def _force_crash(conn: AsyncConnection, job: Job) -> str | None:
        return await force_crash_handler(conn, job, ctrl)

    registry.register(JobKind.POWER, _power)
    registry.register(JobKind.FORCE_CRASH, _force_crash)
