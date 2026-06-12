"""The `control.*` MCP tools (ADR-0028).

`control.power` (``on`` → operator; ``off``/``cycle``/``reset`` → three-check gated,
admin, ADR-0037 §1/§2) and `control.force_crash` (three-check gated, admin) admit
synchronously and enqueue a durable job. Worker-owned execution lives in
``kdive.jobs.handlers.control``; `power` moves no System state (a domain restart is not a
reprovision), while `force_crash` drives System ``ready -> crashed`` and every non-terminal
DebugSession of the System ``-> detached`` (joined through ``runs``).

`power` uses a per-call-unique ``dedup_key`` (``{system_id}:power:{action}:{uuid4}``) so a
repeated power op is always a fresh job; `force_crash` uses a stable
``{system_id}:force_crash`` key (once-per-System: one System per Allocation, no reprovision,
``ready -> crashed`` is one-way).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import DestructiveJobKind, JobKind, PowerAction, System
from kdive.domain.state import SystemState
from kdive.jobs import queue
from kdive.jobs.payloads import PowerPayload, SystemPayload
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
from kdive.mcp.tools._common import job_envelope
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.resolver import ProviderResolver
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed
from kdive.security.authz.rbac import Role, require_role

# Systems that have a started libvirt domain (so a power op has something to act on).
_STARTED_SYSTEM = frozenset({SystemState.READY, SystemState.CRASHED})
_FORCE_CRASH = JobKind.FORCE_CRASH
_POWER = JobKind.POWER
# Power on is a reversible lifecycle move (operator); off/cycle/reset tear into a running
# guest and are destructive-administration ops (admin) — ADR-0037 §1/§2.
_POWER_ON_ACTIONS = frozenset({PowerAction.ON})
_DESTRUCTIVE_POWER_ACTIONS = frozenset({PowerAction.OFF, PowerAction.CYCLE, PowerAction.RESET})


def _power_required_role(action: PowerAction) -> Role:
    """The lowest role that may issue ``action``: ``operator`` for ``on``, else ``admin``."""
    return Role.OPERATOR if action in _POWER_ON_ACTIONS else Role.ADMIN


async def power_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    action: str,
    resolver: ProviderResolver,
) -> ToolResponse:
    """Admit a power op on a started System and enqueue a `power` job.

    ``power on`` requires ``operator`` (a reversible lifecycle move); the destructive
    actions ``off``/``cycle``/``reset`` pass the full destructive-operation gate with the
    ``admin`` role factor (ADR-0037 §1/§2). The checks bind to the target System's project
    and run after the in-project check, so they cannot be evaluated against a foreign
    project.
    """
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
            if power_action in _DESTRUCTIVE_POWER_ACTIONS:
                gated = await _authorize_destructive(
                    conn, ctx, system, uid, _POWER, resolver=resolver, tool="control.power"
                )
                if isinstance(gated, ToolResponse):
                    return gated
            else:
                require_role(ctx, system.project, _power_required_role(power_action))
            if system.state not in _STARTED_SYSTEM:
                return _config_error(system_id, data={"current_status": system.state.value})
            job = await queue.enqueue(
                conn,
                JobKind.POWER,
                PowerPayload(system_id=system_id, action=power_action),
                job_authorizing(ctx, system.project),
                f"{system_id}:power:{power_action.value}:{uuid4()}",
            )
        return job_envelope(job, "system_id", uid)


async def _authorize_destructive(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    system_uid: UUID,
    op_kind: DestructiveJobKind,
    *,
    resolver: ProviderResolver,
    tool: str,
) -> ToolResponse | None:
    allocation = await ALLOCATIONS.get(conn, system.allocation_id)
    if allocation is None or allocation.project not in ctx.projects:
        return _config_error(str(system_uid))
    op = DestructiveOp(
        kind=op_kind, profile_opt_in=await _op_opt_in(conn, system, op_kind, resolver)
    )
    try:
        assert_destructive_allowed(ctx, allocation, op)
    except DestructiveOpDenied as denied:
        async with conn.transaction():
            await audit.record(
                conn,
                ctx,
                audit.AuditEvent(
                    tool=tool,
                    object_kind="systems",
                    object_id=system_uid,
                    transition=f"{op_kind.value}:denied",
                    args={"system_id": str(system_uid), "missing": denied.missing},
                    project=system.project,
                ),
            )
        return ToolResponse.failure(str(system_uid), ErrorCategory.AUTHORIZATION_DENIED)
    return None


async def _op_opt_in(
    conn: AsyncConnection, system: System, op_kind: DestructiveJobKind, resolver: ProviderResolver
) -> bool:
    """Resolve the gate's profile opt-in factor from the System's provisioning profile."""
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    runtime = await resolver.runtime_for_system(conn, system.id)
    return runtime.profile_policy.destructive_opt_in(profile, op_kind)


async def force_crash_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    resolver: ProviderResolver,
) -> ToolResponse:
    """Gate, admit, and enqueue a `force_crash` job for a `ready` System (admin + gate).

    The in-project check precedes the gate, so the denial audit's ``project`` is always in
    ``ctx.projects`` and ``audit.record`` cannot itself raise (ADR-0028 ordering invariant).
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            gated = await _authorize_destructive(
                conn, ctx, system, uid, _FORCE_CRASH, resolver=resolver, tool="control.force_crash"
            )
            if isinstance(gated, ToolResponse):
                return gated
            if system.state is not SystemState.READY:
                return _config_error(system_id, data={"current_status": system.state.value})
            job = await queue.enqueue(
                conn,
                JobKind.FORCE_CRASH,
                SystemPayload(system_id=system_id),
                job_authorizing(ctx, system.project),
                f"{system_id}:force_crash",
            )
        return job_envelope(job, "system_id", uid)


def register(app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver) -> None:
    """Register the `control.*` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="control.power",
        annotations=_docmeta.destructive(),
        meta={"maturity": "partial"},
    )
    async def control_power(
        system_id: Annotated[str, Field(description="The started System to act on.")],
        action: Annotated[
            str,
            Field(description="Power action: `on` (operator) or `off`/`cycle`/`reset` (admin)."),
        ],
    ) -> ToolResponse:
        """Power action on a started System: `on` is reversible (operator); off/cycle/reset
        are destructive (admin). Enqueues a power job."""
        return await power_system(
            pool, current_context(), system_id=system_id, action=action, resolver=resolver
        )

    @app.tool(
        name="control.force_crash",
        annotations=_docmeta.destructive(),
        meta={"maturity": "partial"},
    )
    async def control_force_crash(
        system_id: Annotated[str, Field(description="The ready System to force-crash via NMI.")],
    ) -> ToolResponse:
        """Inject an NMI to crash a ready System; drives ready->crashed. Requires admin + gate."""
        return await force_crash_system(
            pool, current_context(), system_id=system_id, resolver=resolver
        )
