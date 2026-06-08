"""`runs.create` MCP handler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError
from kdive.domain.models import ExpectedBootFailure, Investigation, Run
from kdive.domain.state import InvestigationState, RunState
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import stale_handle as _stale_handle
from kdive.mcp.tools.lifecycle.runs.common import (
    ALLOC_HOSTABLE,
    INVESTIGATION_OPEN_FOR_RUN,
    RUN_HOSTABLE,
    SYSTEM_GONE,
)
from kdive.profiles.build import BuildProfile, ParsedBuildProfile, dump_build_profile
from kdive.profiles.types import (
    BuildProfileInput,
    ExpectedBootFailureInput,
    SerializedExpectedBootFailure,
)
from kdive.security import audit
from kdive.security.context import RequestContext
from kdive.security.rbac import Role, require_role


async def create_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    investigation_id: str,
    system_id: str,
    build_profile: BuildProfileInput,
    expected_boot_failure: ExpectedBootFailureInput | None = None,
) -> ToolResponse:
    """Bind a Run to a `ready` System and an Investigation."""
    inv_uid = _as_uuid(investigation_id)
    if inv_uid is None:
        return _config_error(investigation_id)
    sys_uid = _as_uuid(system_id)
    if sys_uid is None:
        return _config_error(system_id)
    try:
        parsed_build_profile = BuildProfile.parse(build_profile)
    except CategorizedError as exc:
        return ToolResponse.failure(system_id, exc.category)
    parsed_expected = _parse_expected_boot_failure(system_id, expected_boot_failure)
    if isinstance(parsed_expected, ToolResponse):
        return parsed_expected
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, inv_uid)
            if inv is None or inv.project not in ctx.projects:
                return _config_error(investigation_id)
            require_role(ctx, inv.project, Role.OPERATOR)
            system = await SYSTEMS.get(conn, sys_uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            if system.project != inv.project:
                return _config_error(system_id)
            alloc = await ALLOCATIONS.get(conn, system.allocation_id)
            if alloc is None or alloc.state not in ALLOC_HOSTABLE:
                current = alloc.state.value if alloc is not None else "missing"
                return _stale_handle(system_id, current_status=current)
            return await _create_locked(
                conn,
                ctx,
                inv_uid,
                sys_uid,
                parsed_build_profile,
                parsed_expected,
                project=inv.project,
            )


def _parse_expected_boot_failure(
    object_id: str, value: ExpectedBootFailureInput | None
) -> SerializedExpectedBootFailure | ToolResponse | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return _config_error(object_id, data={"reason": "bad_expected_boot_failure"})
    try:
        parsed = ExpectedBootFailure.model_validate(value)
    except ValidationError:
        return _config_error(object_id, data={"reason": "bad_expected_boot_failure"})
    return cast(
        SerializedExpectedBootFailure,
        parsed.model_dump(mode="json", exclude_none=True),
    )


async def _investigation_for_update(conn: AsyncConnection, uid: UUID) -> Investigation | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return Investigation.model_validate(row) if row else None


async def _create_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    inv_uid: UUID,
    sys_uid: UUID,
    build_profile: ParsedBuildProfile,
    expected_boot_failure: SerializedExpectedBootFailure | None,
    *,
    project: str,
) -> ToolResponse:
    now = datetime.now(UTC)
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, sys_uid),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, inv_uid),
    ):
        system = await SYSTEMS.get(conn, sys_uid)
        if system is None:
            return _config_error(str(sys_uid))
        if system.state in SYSTEM_GONE:
            return _stale_handle(str(sys_uid), current_status=system.state.value)
        if system.state not in RUN_HOSTABLE:
            return _config_error(str(sys_uid), data={"current_status": system.state.value})
        inv = await _investigation_for_update(conn, inv_uid)
        if inv is None:
            return _config_error(str(inv_uid))
        if inv.state not in INVESTIGATION_OPEN_FOR_RUN:
            return _config_error(str(inv_uid), data={"current_status": inv.state.value})
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=now,
                updated_at=now,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=project,
                investigation_id=inv_uid,
                system_id=sys_uid,
                state=RunState.CREATED,
                build_profile=dump_build_profile(build_profile),
                expected_boot_failure=expected_boot_failure,
            ),
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="runs.create",
                object_kind="runs",
                object_id=run.id,
                transition="->created",
                args={"investigation_id": str(inv_uid), "system_id": str(sys_uid)},
                project=project,
            ),
        )
        if inv.state is InvestigationState.OPEN:
            await INVESTIGATIONS.update_state(conn, inv_uid, InvestigationState.ACTIVE)
            await audit.record(
                conn,
                ctx,
                audit.AuditEvent(
                    tool="runs.create",
                    object_kind="investigations",
                    object_id=inv_uid,
                    transition="open->active",
                    args={"investigation_id": str(inv_uid)},
                    project=project,
                ),
            )
        await conn.execute(
            "UPDATE investigations SET last_run_at = now() WHERE id = %s", (inv_uid,)
        )
    return ToolResponse.success(
        str(run.id),
        "created",
        suggested_next_actions=["runs.get", "runs.build"],
        data={
            "project": project,
            "investigation_id": str(inv_uid),
            "system_id": str(sys_uid),
            **(
                {"expected_boot_failure": str(expected_boot_failure["kind"])}
                if expected_boot_failure is not None
                else {}
            ),
        },
    )
