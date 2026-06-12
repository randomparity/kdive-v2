"""Worker handlers for the `control.*` plane."""

from __future__ import annotations

import asyncio
from typing import NamedTuple
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle_rules import TERMINAL_SYSTEM_STATES
from kdive.domain.models import Job, JobKind, System
from kdive.domain.state import DebugSessionState, SystemState
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import PowerPayload, SystemPayload, load_payload
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime_paths import domain_name_for
from kdive.security import audit


class _ControlTarget(NamedTuple):
    domain_name: str
    project: str


def domain_name(system: System) -> str:
    return system.domain_name or domain_name_for(system.id)


async def _control_target(conn: AsyncConnection, system_id: UUID, *, op: str) -> _ControlTarget:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                f"{op} target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        return _ControlTarget(domain_name(system), system.project)


async def _controller(conn: AsyncConnection, system_id: UUID, resolver: ProviderResolver):
    """Resolve the System's controller port."""
    return (await resolver.runtime_for_system(conn, system_id)).controller


async def power_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
) -> str | None:
    """Drive the domain's power; audit `power:{action}`; move no System state."""
    payload = load_payload(job, PowerPayload)
    system_id = UUID(payload.system_id)
    action = payload.action
    target = await _control_target(conn, system_id, op="power")
    control = await _controller(conn, system_id, resolver)
    await asyncio.to_thread(control.power, target.domain_name, action)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "power target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        await audit.record(
            conn,
            job_context_from_job(job, target.project),
            audit.AuditEvent(
                tool="control.power",
                object_kind="systems",
                object_id=system_id,
                transition=f"power:{action.value}",
                args={"system_id": str(system_id), "action": action.value},
                project=target.project,
            ),
        )
    return str(system_id)


async def force_crash_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
) -> str | None:
    """Crash the guest and drive System ready->crashed + DebugSession live->detached."""
    system_id = UUID(load_payload(job, SystemPayload).system_id)
    target = await _force_crash_target(conn, system_id)
    if target is None:
        return str(system_id)
    control = await _controller(conn, system_id, resolver)
    await asyncio.to_thread(control.force_crash, target.domain_name)
    await _finalize_force_crash(conn, job, system_id, target.project)
    return str(system_id)


async def _force_crash_target(conn: AsyncConnection, system_id: UUID) -> _ControlTarget | None:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "force_crash target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        if system.state in TERMINAL_SYSTEM_STATES:
            return None
        return _ControlTarget(domain_name(system), system.project)


async def _finalize_force_crash(
    conn: AsyncConnection, job: Job, system_id: UUID, project: str
) -> None:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "force_crash target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        if system.state in TERMINAL_SYSTEM_STATES:
            return
        if system.state is SystemState.READY:
            await SYSTEMS.update_state(conn, system_id, SystemState.CRASHED)
            await audit.record(
                conn,
                job_context_from_job(job, project),
                audit.AuditEvent(
                    tool="control.force_crash",
                    object_kind="systems",
                    object_id=system_id,
                    transition="ready->crashed",
                    args={"system_id": str(system_id)},
                    project=project,
                ),
            )
        await detach_sessions(conn, job, system)


async def detach_sessions(conn: AsyncConnection, job: Job, system: System) -> None:
    """Drive every non-terminal DebugSession of ``system`` to detached."""
    async with conn.cursor() as cur:
        await cur.execute(
            "WITH targets AS ("
            "    SELECT id, state FROM debug_sessions "
            "    WHERE state IN (%s, %s) "
            "      AND run_id IN (SELECT id FROM runs WHERE system_id = %s) "
            "    FOR UPDATE"
            ") "
            "UPDATE debug_sessions s SET state = %s "
            "FROM targets t WHERE s.id = t.id "
            "RETURNING s.id, t.state",
            (
                DebugSessionState.ATTACH.value,
                DebugSessionState.LIVE.value,
                system.id,
                DebugSessionState.DETACHED.value,
            ),
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
    resolver: ProviderResolver,
) -> None:
    """Bind the `power`/`force_crash` job handlers."""
    registry.register(JobKind.POWER, lambda conn, job: power_handler(conn, job, resolver=resolver))
    registry.register(
        JobKind.FORCE_CRASH,
        lambda conn, job: force_crash_handler(conn, job, resolver=resolver),
    )
