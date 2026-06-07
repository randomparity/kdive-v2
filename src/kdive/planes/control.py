"""Worker handlers for the `control.*` plane."""

from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, System
from kdive.domain.state import SystemState
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import PowerPayload, SystemPayload, load_payload
from kdive.mcp.job_context import context_from_job as job_context_from_job
from kdive.providers.composition import ProviderRuntime, controller_from_env, domain_name_for
from kdive.providers.ports import Controller, PowerAction
from kdive.security import audit

TERMINAL_SYSTEM = frozenset({SystemState.TORN_DOWN, SystemState.FAILED})


def domain_name(system: System) -> str:
    return system.domain_name or domain_name_for(system.id)


async def power_handler(conn: AsyncConnection, job: Job, control: Controller) -> str | None:
    """Drive the domain's power; audit `power:{action}`; move no System state."""
    payload = load_payload(job, PowerPayload)
    system_id = UUID(payload.system_id)
    action = PowerAction(payload.action)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "power target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        control.power(domain_name(system), action)
        await audit.record(
            conn,
            job_context_from_job(job, system.project),
            audit.AuditEvent(
                tool="control.power",
                object_kind="systems",
                object_id=system_id,
                transition=f"power:{action.value}",
                args={"system_id": str(system_id), "action": action.value},
                project=system.project,
            ),
        )
    return str(system_id)


async def force_crash_handler(conn: AsyncConnection, job: Job, control: Controller) -> str | None:
    """Crash the guest and drive System ready->crashed + DebugSession live->detached."""
    system_id = UUID(load_payload(job, SystemPayload).system_id)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "force_crash target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        if system.state in TERMINAL_SYSTEM:
            return str(system_id)
        control.force_crash(domain_name(system))
        if system.state is SystemState.READY:
            await SYSTEMS.update_state(conn, system_id, SystemState.CRASHED)
            await audit.record(
                conn,
                job_context_from_job(job, system.project),
                audit.AuditEvent(
                    tool="control.force_crash",
                    object_kind="systems",
                    object_id=system_id,
                    transition="ready->crashed",
                    args={"system_id": str(system_id)},
                    project=system.project,
                ),
            )
        await detach_sessions(conn, job, system)
    return str(system_id)


async def detach_sessions(conn: AsyncConnection, job: Job, system: System) -> None:
    """Drive every non-terminal DebugSession of ``system`` to detached."""
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
            job_context_from_job(job, system.project),
            audit.AuditEvent(
                tool="control.force_crash",
                object_kind="debug_sessions",
                object_id=session_id,
                transition=f"{old_state}->detached",
                args={"system_id": str(system.id)},
                project=system.project,
            ),
        )


def register_handlers(
    registry: HandlerRegistry,
    *,
    control: Controller | None = None,
    provider_runtime: ProviderRuntime | None = None,
) -> None:
    """Bind the `power`/`force_crash` job handlers; build the provider lazily from env."""
    ctrl = control or (provider_runtime.controller() if provider_runtime else controller_from_env())

    async def _power(conn: AsyncConnection, job: Job) -> str | None:
        return await power_handler(conn, job, ctrl)

    async def _force_crash(conn: AsyncConnection, job: Job) -> str | None:
        return await force_crash_handler(conn, job, ctrl)

    registry.register(JobKind.POWER, _power)
    registry.register(JobKind.FORCE_CRASH, _force_crash)
