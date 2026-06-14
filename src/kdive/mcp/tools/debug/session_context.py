"""Shared debug-session lookup and authorization boundary."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.repositories import DEBUG_SESSIONS, RUNS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import DebugSession
from kdive.domain.state import DebugSessionState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.providers.ports import DebugTransportKind
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role


@dataclass(frozen=True)
class DebugSessionContext:
    """Resolved debug-session boundary data after authz and state checks."""

    session: DebugSession
    session_id: UUID
    project: str
    transport_handle: str | None
    system_id: UUID | None = None


def debug_session_error(
    session_id: str, code: str, *, current_status: str | None = None
) -> ToolResponse:
    """Return the standard debug-session configuration failure envelope."""
    data = {"code": code}
    if current_status is not None:
        data["current_status"] = current_status
    return ToolResponse.failure(session_id, ErrorCategory.CONFIGURATION_ERROR, data=data)


async def resolve_debug_session_context(
    conn: AsyncConnection,
    ctx: RequestContext,
    session_id: str,
    *,
    required_transport: DebugTransportKind | None = None,
    require_live: bool = False,
    include_system: bool = False,
) -> DebugSessionContext | ToolResponse:
    """Load a DebugSession, enforce project/operator gates, and optionally require live state."""
    uid = _as_uuid(session_id)
    if uid is None:
        return debug_session_error(session_id, "bad_session_id")
    session = await DEBUG_SESSIONS.get(conn, uid)
    if session is None or session.project not in ctx.projects:
        return debug_session_error(session_id, "unknown_session")
    require_role(ctx, session.project, Role.OPERATOR)
    if require_live and session.state is not DebugSessionState.LIVE:
        return debug_session_error(session_id, "not_live", current_status=session.state.value)
    if required_transport is not None and session.transport != required_transport:
        return debug_session_error(session_id, "wrong_transport")
    if required_transport is not None and session.transport_handle is None:
        return debug_session_error(session_id, "missing_transport_handle")
    system_id: UUID | None = None
    if include_system:
        run = await RUNS.get(conn, session.run_id)
        if run is None:
            return debug_session_error(session_id, "unknown_session")
        system_id = run.system_id
    return DebugSessionContext(
        session=session,
        session_id=uid,
        project=session.project,
        transport_handle=session.transport_handle,
        system_id=system_id,
    )
