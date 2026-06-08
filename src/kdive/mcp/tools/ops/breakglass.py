"""Break-glass `ops.force_teardown` / `ops.force_release` MCP tools (ADR-0062 §4).

A ``platform_admin`` break-glass path that **fully overrides** the three-check destructive
gate (`assert_destructive_allowed`): the gate protects a member operating within their own
project, and a stuck cross-project object typically fails all three checks, which is exactly
when break-glass is needed. Authority comes solely from ``require_platform_role(PLATFORM_ADMIN)``
+ a non-blank ``reason`` + an always-written ``platform_audit_log`` row.

These reuse the per-project tools' teardown/release **mechanics** (`release_with_backstops`
for the release transition; the `JobKind.TEARDOWN` enqueue for teardown) but not their
authorization or audit attribution: `audit.record` enforces project membership and a
break-glass admin is never a member, so the release path writes its per-allocation audit rows
through the guard-exempt `audit.record_system` writer, recording the platform principal against
the target's project. The per-project `systems.teardown` / `allocations.release` are unchanged.

The `platform_audit_log` row is the sole accountability mechanism for a gate-bypassing tool, so
it is written in its own committed transaction **before** the release/teardown mechanic runs:
a rolled-back or failed release never rolls back the accountability record.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.models import Job, JobKind
from kdive.domain.state import SystemState
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import job_envelope
from kdive.mcp.tools.lifecycle.allocations import AuditWriter, release_with_backstops
from kdive.security import audit
from kdive.security.context import RequestContext
from kdive.security.rbac import PlatformRole, require_platform_role

_log = logging.getLogger(__name__)

_FORCE_RELEASE_TOOL = "ops.force_release"
_FORCE_TEARDOWN_TOOL = "ops.force_teardown"


def _breakglass_audit_writer(principal: str) -> AuditWriter:
    """Guard-exempt writer: `record_system` under the platform principal (no membership guard)."""

    async def _write(conn: AsyncConnection, event: audit.AuditEvent) -> None:
        await audit.record_system(conn, principal=principal, event=event)

    return _write


def _held_platform_roles(ctx: RequestContext) -> str | None:
    if not ctx.platform_roles:
        return None
    return ",".join(sorted(r.value for r in ctx.platform_roles))


def _blank(reason: str) -> bool:
    return not reason.strip()


async def _record_breakglass(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    project: str,
    object_id: str,
    reason: str,
) -> None:
    """Write the always-on `platform_audit_log` row in its own committed transaction.

    The reason rides the ``args`` digest input (tamper-evident, never plaintext); ``scope``
    pairs the target project with the object id so the row is attributable cross-tenant.
    """
    scope = f"{project}:{object_id}"
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=tool,
                scope=scope,
                args={"object_id": object_id, "reason": reason},
                platform_role=_held_platform_roles(ctx),
            ),
        )


async def force_release(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    allocation_id: str,
    reason: str,
) -> ToolResponse:
    """Break-glass release of a stuck cross-project allocation (``platform_admin``).

    Bypasses the three-check destructive gate entirely. Authority = platform_admin + a
    non-blank ``reason`` + the always-written ``platform_audit_log`` row. The per-allocation
    release transition rows are audited under the platform principal via the guard-exempt
    writer (the admin is not a project member). A reconcile failure or stale handle returns a
    typed envelope; the accountability row is written regardless.
    """
    require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    with bind_context(principal=ctx.principal):
        if _blank(reason):
            return _config_error(allocation_id)
        uid = _as_uuid(allocation_id)
        if uid is None:
            return _config_error(allocation_id)
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
        if alloc is None:
            return _config_error(allocation_id)
        await _record_breakglass(
            pool,
            ctx,
            tool=_FORCE_RELEASE_TOOL,
            project=alloc.project,
            object_id=str(uid),
            reason=reason,
        )
        _log.warning(
            "break-glass force_release of allocation %s in project %s by %s",
            uid,
            alloc.project,
            ctx.principal,
        )
        return await release_with_backstops(
            pool,
            uid,
            project=alloc.project,
            audit_writer=_breakglass_audit_writer(ctx.principal),
        )


async def force_teardown(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    reason: str,
) -> ToolResponse:
    """Break-glass teardown of a stuck cross-project System (``platform_admin``).

    Bypasses the three-check destructive gate entirely. Authority = platform_admin + a
    non-blank ``reason`` + the always-written ``platform_audit_log`` row. Enqueues the same
    idempotent ``JobKind.TEARDOWN`` job as ``systems.teardown`` (same dedup key) under an
    authorizing context bound to the target's project; a terminal System returns success
    idempotently.
    """
    require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    with bind_context(principal=ctx.principal):
        if _blank(reason):
            return _config_error(system_id)
        uid = _as_uuid(system_id)
        if uid is None:
            return _config_error(system_id)
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
        if system is None:
            return _config_error(system_id)
        await _record_breakglass(
            pool,
            ctx,
            tool=_FORCE_TEARDOWN_TOOL,
            project=system.project,
            object_id=str(uid),
            reason=reason,
        )
        _log.warning(
            "break-glass force_teardown of system %s in project %s by %s",
            uid,
            system.project,
            ctx.principal,
        )
        return await _teardown_locked(pool, ctx, uid)


async def _teardown_locked(
    pool: AsyncConnectionPool, ctx: RequestContext, uid: UUID
) -> ToolResponse:
    """Read state, short-circuit `torn_down`, and enqueue — all under the System lock.

    Mirrors `systems.teardown`'s concurrency discipline (`lifecycle/systems/admin.py`): the
    `advisory_xact_lock(SYSTEM)` spans the authoritative state read, the idempotent
    `torn_down` short-circuit, and the enqueue, so a concurrent teardown cannot slip a state
    change between the check and the enqueue. The `{uid}:teardown` dedup key keeps it
    re-invokable regardless. The `platform_audit_log` row is already committed by the caller.
    """
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, uid),
    ):
        system = await SYSTEMS.get(conn, uid)
        if system is None:  # Resolved a moment ago; a concurrent delete is a stale handle.
            return _config_error(str(uid))
        if system.state is SystemState.TORN_DOWN:
            return ToolResponse.success(
                str(uid),
                "torn_down",
                suggested_next_actions=["systems.get"],
                data={"project": system.project},
            )
        job = await _enqueue_teardown(conn, ctx, uid, system.project)
    return job_envelope(job, "system_id", uid)


async def _enqueue_teardown(
    conn: AsyncConnection, ctx: RequestContext, system_id: UUID, project: str
) -> Job:
    """Enqueue the idempotent teardown job under the target's project (break-glass attribution)."""
    return await queue.enqueue(
        conn,
        JobKind.TEARDOWN,
        {"system_id": str(system_id)},
        {"principal": ctx.principal, "agent_session": ctx.agent_session, "project": project},
        f"{system_id}:teardown",
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the break-glass `ops.*` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="ops.force_release",
        annotations=_docmeta.destructive(),
        meta={"maturity": "implemented"},
    )
    async def ops_force_release(
        allocation_id: Annotated[
            str, Field(description="The cross-project Allocation to release.")
        ],
        reason: Annotated[
            str, Field(description="Mandatory non-blank break-glass justification (audited).")
        ],
    ) -> ToolResponse:
        """Break-glass release of a stuck cross-project Allocation. Requires platform_admin."""
        return await force_release(
            pool, current_context(), allocation_id=allocation_id, reason=reason
        )

    @app.tool(
        name="ops.force_teardown",
        annotations=_docmeta.destructive(),
        meta={"maturity": "implemented"},
    )
    async def ops_force_teardown(
        system_id: Annotated[str, Field(description="The cross-project System to tear down.")],
        reason: Annotated[
            str, Field(description="Mandatory non-blank break-glass justification (audited).")
        ],
    ) -> ToolResponse:
        """Break-glass teardown of a stuck cross-project System. Requires platform_admin."""
        return await force_teardown(pool, current_context(), system_id=system_id, reason=reason)
