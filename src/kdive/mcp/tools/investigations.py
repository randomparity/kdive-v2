"""The `investigations.*` MCP tools — the Investigation campaign surface (ADR-0026).

Thin FastMCP wrappers over plain async handlers (pool + ctx injected; tested directly).
`open` mints an Investigation (`open`); `close` drives it to `closed`; `link`/`unlink`
mutate the `external_refs` jsonb under a per-Investigation advisory lock, keyed on the
`(tracker, id)` natural key (link upserts, unlink removes-if-present — both idempotent).
`get`/the mutators render through `_envelope_for_investigation` (every Investigation state
is a non-failure status, so no failure mapping is needed). RBAC: mutations require
`operator`; reads require project membership. Authz denials raise (ADR-0020: no authz
ErrorCategory).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import INVESTIGATIONS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import ExternalRef, Investigation
from kdive.domain.state import IllegalTransition, InvestigationState
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context, require_project
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.rbac import Role, require_role

_TERMINAL_INVESTIGATION = frozenset({InvestigationState.CLOSED, InvestigationState.ABANDONED})


def _config_error(object_id: str, *, data: dict[str, str] | None = None) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _envelope_for_investigation(inv: Investigation) -> ToolResponse:
    """Render an Investigation; every state is a non-failure status (ADR-0026 §6)."""
    if inv.state in _TERMINAL_INVESTIGATION:
        actions = ["investigations.get"]
    else:
        actions = ["investigations.get", "investigations.close", "runs.create"]
    return ToolResponse.success(
        str(inv.id),
        inv.state.value,
        suggested_next_actions=actions,
        data={"project": inv.project, "external_refs": str(len(inv.external_refs))},
    )


def _parse_external_refs(raw: list[dict[str, Any]] | None) -> list[ExternalRef]:
    """Parse + dedup external refs by the ``(tracker, id)`` natural key (last-wins).

    Raises:
        ValidationError / TypeError: A malformed entry or a non-list container.
    """
    if raw is None:
        return []
    by_key: dict[tuple[str, str], ExternalRef] = {}
    for entry in raw:
        ref = ExternalRef.model_validate(entry)
        by_key[(ref.tracker, ref.id)] = ref
    return list(by_key.values())


async def open_investigation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    title: str,
    external_refs: list[dict[str, Any]] | None = None,
) -> ToolResponse:
    """Mint an Investigation (`open`) for the caller's project."""
    require_project(ctx, project)
    require_role(ctx, project, Role.OPERATOR)
    with bind_context(principal=ctx.principal):
        try:
            refs = _parse_external_refs(external_refs)
        except (ValidationError, TypeError):
            return _config_error(project)
        now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
        async with pool.connection() as conn, conn.transaction():
            inv = await INVESTIGATIONS.insert(
                conn,
                Investigation(
                    id=uuid4(),
                    created_at=now,
                    updated_at=now,
                    principal=ctx.principal,
                    agent_session=ctx.agent_session,
                    project=project,
                    title=title,
                    external_refs=refs,
                    state=InvestigationState.OPEN,
                ),
            )
            await audit.record(
                conn,
                ctx,
                tool="investigations.open",
                object_kind="investigations",
                object_id=inv.id,
                transition="->open",
                args={"project": project, "title": title},
                project=project,
            )
        return ToolResponse.success(
            str(inv.id),
            "open",
            suggested_next_actions=["investigations.get", "runs.create"],
            data={"project": project},
        )


async def get_investigation(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str
) -> ToolResponse:
    """Return an Investigation the caller's project owns, or a not-found-shaped error."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _config_error(investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, uid)
        if inv is None or inv.project not in ctx.projects:
            return _config_error(investigation_id)
        return _envelope_for_investigation(inv)


async def _close_locked(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, *, project: str
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await INVESTIGATIONS.get(conn, uid)
        if current is None:
            return _config_error(str(uid))
        if current.state is InvestigationState.CLOSED:
            return ToolResponse.success(
                str(uid),
                "closed",
                suggested_next_actions=["investigations.get"],
                data={"project": project},
            )
        if current.state is InvestigationState.ABANDONED:
            return _config_error(str(uid), data={"current_status": "abandoned"})
        old = current.state
        await INVESTIGATIONS.update_state(conn, uid, InvestigationState.CLOSED)
        await audit.record(
            conn,
            ctx,
            tool="investigations.close",
            object_kind="investigations",
            object_id=uid,
            transition=f"{old.value}->closed",
            args={"investigation_id": str(uid)},
            project=project,
        )
    return ToolResponse.success(
        str(uid), "closed", suggested_next_actions=["investigations.get"], data={"project": project}
    )


async def close_investigation(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str
) -> ToolResponse:
    """Drive an Investigation to `closed` (idempotent on an already-`closed` row)."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _config_error(investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, uid)
            if inv is None or inv.project not in ctx.projects:
                return _config_error(investigation_id)
            require_role(ctx, inv.project, Role.OPERATOR)
            try:
                return await _close_locked(conn, ctx, uid, project=inv.project)
            except IllegalTransition:
                # Backstop for an interleaving the lock did not cover (e.g. a future
                # non-advisory writer). Caught OUTSIDE the rolled-back transaction; re-read.
                async with pool.connection() as conn2:
                    latest = await INVESTIGATIONS.get(conn2, uid)
                data = {"current_status": latest.state.value} if latest else {}
                return _config_error(investigation_id, data=data)


async def _get_for_update(conn: AsyncConnection, uid: UUID) -> Investigation | None:
    """Read an Investigation row ``FOR UPDATE`` (held under the per-Investigation lock)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return Investigation.model_validate(row) if row else None


def _natural_key(ref: dict[str, Any]) -> tuple[str, str] | None:
    """The ``(tracker, id)`` identity of a ref input; ``None`` if either is missing/blank."""
    tracker = ref.get("tracker")
    rid = ref.get("id")
    if not isinstance(tracker, str) or not tracker:
        return None
    if not isinstance(rid, str) or not rid:
        return None
    return (tracker, rid)


def _refs_jsonb(refs: list[ExternalRef]) -> Jsonb:
    return Jsonb([r.model_dump() for r in refs])


async def _link_locked(
    conn: AsyncConnection, ctx: RequestContext, uid: UUID, ref: ExternalRef, *, project: str
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await _get_for_update(conn, uid)
        if current is None:
            return _config_error(str(uid))
        if current.state in _TERMINAL_INVESTIGATION:
            return _config_error(str(uid), data={"current_status": current.state.value})
        kept = [r for r in current.external_refs if (r.tracker, r.id) != (ref.tracker, ref.id)]
        kept.append(ref)
        await conn.execute(
            "UPDATE investigations SET external_refs = %s WHERE id = %s", (_refs_jsonb(kept), uid)
        )
        await audit.record(
            conn,
            ctx,
            tool="investigations.link",
            object_kind="investigations",
            object_id=uid,
            transition="link",
            args={"tracker": ref.tracker, "id": ref.id},
            project=project,
        )
        updated = current.model_copy(update={"external_refs": kept})
    return _envelope_for_investigation(updated)


async def _unlink_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    uid: UUID,
    key: tuple[str, str],
    *,
    project: str,
) -> ToolResponse:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.INVESTIGATION, uid):
        current = await _get_for_update(conn, uid)
        if current is None:
            return _config_error(str(uid))
        if current.state in _TERMINAL_INVESTIGATION:
            return _config_error(str(uid), data={"current_status": current.state.value})
        kept = [r for r in current.external_refs if (r.tracker, r.id) != key]
        if len(kept) != len(current.external_refs):
            await conn.execute(
                "UPDATE investigations SET external_refs = %s WHERE id = %s",
                (_refs_jsonb(kept), uid),
            )
            await audit.record(
                conn,
                ctx,
                tool="investigations.unlink",
                object_kind="investigations",
                object_id=uid,
                transition="unlink",
                args={"tracker": key[0], "id": key[1]},
                project=project,
            )
        updated = current.model_copy(update={"external_refs": kept})
    return _envelope_for_investigation(updated)


async def link_external_ref(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str, ref: dict[str, Any]
) -> ToolResponse:
    """Upsert an external ref onto an Investigation (keyed on `(tracker, id)`)."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _config_error(investigation_id)
    try:
        parsed = ExternalRef.model_validate(ref)
    except ValidationError:
        return _config_error(investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, uid)
            if inv is None or inv.project not in ctx.projects:
                return _config_error(investigation_id)
            require_role(ctx, inv.project, Role.OPERATOR)
            return await _link_locked(conn, ctx, uid, parsed, project=inv.project)


async def unlink_external_ref(
    pool: AsyncConnectionPool, ctx: RequestContext, investigation_id: str, ref: dict[str, Any]
) -> ToolResponse:
    """Remove an external ref by its `(tracker, id)` key (idempotent; `url` ignored)."""
    uid = _as_uuid(investigation_id)
    if uid is None:
        return _config_error(investigation_id)
    key = _natural_key(ref)
    if key is None:
        return _config_error(investigation_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, uid)
            if inv is None or inv.project not in ctx.projects:
                return _config_error(investigation_id)
            require_role(ctx, inv.project, Role.OPERATOR)
            return await _unlink_locked(conn, ctx, uid, key, project=inv.project)


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `investigations.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="investigations.open")
    async def investigations_open(
        project: str, title: str, external_refs: list[dict[str, Any]] | None = None
    ) -> ToolResponse:
        return await open_investigation(
            pool, current_context(), project=project, title=title, external_refs=external_refs
        )

    @app.tool(name="investigations.get")
    async def investigations_get(investigation_id: str) -> ToolResponse:
        return await get_investigation(pool, current_context(), investigation_id)

    @app.tool(name="investigations.close")
    async def investigations_close(investigation_id: str) -> ToolResponse:
        return await close_investigation(pool, current_context(), investigation_id)

    @app.tool(name="investigations.link")
    async def investigations_link(investigation_id: str, ref: dict[str, Any]) -> ToolResponse:
        return await link_external_ref(pool, current_context(), investigation_id, ref)

    @app.tool(name="investigations.unlink")
    async def investigations_unlink(investigation_id: str, ref: dict[str, Any]) -> ToolResponse:
        return await unlink_external_ref(pool, current_context(), investigation_id, ref)
