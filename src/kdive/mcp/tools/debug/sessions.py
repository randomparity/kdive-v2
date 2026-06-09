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
`force_crash`/reboot `live -> detached` path is the control plane's
`_detach_sessions`); this module owns only the agent-initiated start/end.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, LiteralString
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import DEBUG_SESSIONS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import DebugSession, Run, System
from kdive.domain.state import DebugSessionState, RunState, SystemState
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.debug.ops import (
    DebugEngineRuntime,
    DebugRuntimeResolver,
    _register_debug_ops,
)
from kdive.profiles.provisioning import ProvisioningProfile, ssh_credential_ref
from kdive.providers.ports import Connector, SystemHandle, TransportHandle
from kdive.providers.resolver import ProviderBinding, ProviderResolver
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.security.secrets.paths import PathSafetyError
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_GDBSTUB = "gdbstub"
_SSH = "ssh"
_TRANSPORTS = frozenset({_GDBSTUB, _SSH})
# An attach failure maps these provider categories onto the response envelope. A
# MISSING_DEPENDENCY (no live_vm host / unresolvable endpoint) surfaces as an attach failure:
# the agent cannot attach either way.
_ATTACH_FAILURE = frozenset({ErrorCategory.DEBUG_ATTACH_FAILURE, ErrorCategory.TRANSPORT_FAILURE})

# A live/attach session occupies the System's single endpoint **for that transport kind**
# (single-attach per transport, ADR-0039 §4): a gdbstub and an ssh session may coexist on one
# System, but a second attach over the same transport is `transport_conflict`.
_OCCUPIED_SQL: LiteralString = (
    "SELECT 1 FROM debug_sessions s "
    "JOIN runs r ON r.id = s.run_id "
    "WHERE r.system_id = %s AND s.transport = %s AND s.state IN ('attach', 'live') LIMIT 1"
)


@dataclass(frozen=True)
class _AttachRequest:
    run: Run
    system: System
    session_id: UUID
    transport: str
    connector: Connector


def _secret_scope(session_id: UUID) -> str:
    return f"debug-session:{session_id}"


async def _system_for_run(conn: AsyncConnection, run: Run) -> System | None:
    return await SYSTEMS.get(conn, run.system_id)


async def _succeeded_boot_result(conn: AsyncConnection, run_id: UUID) -> dict[str, Any] | None:
    """Return the succeeded ``boot`` step result for ``run_id`` when one exists."""
    query: LiteralString = (
        "SELECT result FROM run_steps WHERE run_id = %s AND step = 'boot' AND state = 'succeeded'"
    )
    async with conn.cursor() as cur:
        await cur.execute(query, (run_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    result = row[0]
    return result if isinstance(result, dict) else {}


async def _system_occupied(conn: AsyncConnection, system_id: UUID, transport: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(_OCCUPIED_SQL, (system_id, transport))
        return await cur.fetchone() is not None


async def _open_transport(
    connector: Connector, system: System, transport: str
) -> TransportHandle | ToolResponse:
    """Open the transport outside any lock; map a provider failure to an envelope."""
    handle_name = system.domain_name or str(system.id)
    try:
        return await asyncio.to_thread(
            connector.open_transport, SystemHandle(handle_name), transport
        )
    except CategorizedError as exc:
        category = (
            exc.category
            if exc.category in _ATTACH_FAILURE
            else _map_attach_failure_category(exc.category)
        )
        return ToolResponse.failure_from_error(str(system.id), exc, category=category)


def _map_attach_failure_category(category: ErrorCategory) -> ErrorCategory:
    """Map a non-attach provider category onto the response taxonomy (MISSING_DEPENDENCY)."""
    if category is ErrorCategory.MISSING_DEPENDENCY:
        return ErrorCategory.DEBUG_ATTACH_FAILURE
    return category


def _release_failed_attach_secret(
    registry: SecretRegistry, secret_scope: str, result: ToolResponse | TransportHandle
) -> None:
    if isinstance(result, ToolResponse) and result.status != "live":
        registry.release(secret_scope)


class DebugSessionHandlers:
    """Bound debug session lifecycle handlers.

    The public methods take only MCP-facing inputs; provider and test seams are bound once
    at construction, matching the lifecycle handler pattern used by runs and systems.
    """

    def __init__(
        self,
        connector_source: ProviderResolver | Connector,
        *,
        runtime: DebugEngineRuntime | DebugRuntimeResolver | None = None,
        secret_backend_factory: Callable[[UUID], SecretBackend] | None = None,
        secret_registry: SecretRegistry,
    ) -> None:
        self._connector_source = connector_source
        self._runtime = runtime
        self._secret_backend_factory = secret_backend_factory
        self._secret_registry = secret_registry

    async def start_session(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        run_id: str,
        transport: str = _GDBSTUB,
    ) -> ToolResponse:
        """Open a single-attach transport and insert a `live` DebugSession (operator).

        For ``transport="ssh"`` the guest credential is resolved from the System's profile
        ``ssh_credential_ref`` through the bound secret backend factory **before** the
        transport is opened, so the redaction registry is seeded before any transport output
        can carry the value (ADR-0039 §2). gdbstub needs no credential.
        """
        uid = _as_uuid(run_id)
        if uid is None:
            return _config_error(run_id)
        if transport not in _TRANSPORTS:
            return _config_error(run_id)
        session_id = uuid4()
        secret_scope = _secret_scope(session_id)
        with bind_context(principal=ctx.principal):
            async with pool.connection() as conn:
                request = await self._prepare_attach_request(conn, ctx, uid, transport, session_id)
            if isinstance(request, ToolResponse):
                return request
            opened = await _open_transport(request.connector, request.system, request.transport)
            _release_failed_attach_secret(self._secret_registry, secret_scope, opened)
            if isinstance(opened, ToolResponse):
                return opened
            async with pool.connection() as conn:
                response = await _insert_session_locked(
                    conn,
                    ctx,
                    request.run,
                    request.system,
                    opened,
                    request.connector,
                    request.transport,
                    request.session_id,
                )
            _release_failed_attach_secret(self._secret_registry, secret_scope, response)
            return response

    async def _prepare_attach_request(
        self,
        conn: AsyncConnection,
        ctx: RequestContext,
        run_id: UUID,
        transport: str,
        session_id: UUID,
    ) -> _AttachRequest | ToolResponse:
        run = await RUNS.get(conn, run_id)
        if run is None or run.project not in ctx.projects:
            return _config_error(str(run_id))
        require_role(ctx, run.project, Role.OPERATOR)
        guard = await _attach_preconditions(conn, run, transport)
        if isinstance(guard, ToolResponse):
            return guard
        backend = self._credential_backend(session_id, transport)
        resolved = _resolve_credential(guard, transport, backend)
        if isinstance(resolved, ToolResponse):
            return resolved
        if isinstance(self._connector_source, ProviderResolver):
            try:
                runtime = await self._connector_source.runtime_for_run(conn, run.id)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(str(run.id), exc)
            connector = runtime.connector
        else:
            connector = self._connector_source
        return _AttachRequest(
            run=run,
            system=guard,
            session_id=session_id,
            transport=transport,
            connector=connector,
        )

    def _credential_backend(self, session_id: UUID, transport: str) -> SecretBackend | None:
        if transport != _SSH or self._secret_backend_factory is None:
            return None
        return self._secret_backend_factory(session_id)

    async def end_session(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        session_id: str,
    ) -> ToolResponse:
        """Drive a live/attach DebugSession `-> detached` (idempotent on detached; operator).

        Also reaps the lazy gdb-MI engine (ADR-0034 §4d): under the per-session lock it exits
        the gdb subprocess and drops the registry entry, so an ended session never strands a
        subprocess or holds the single-attach stub. Reaping a session that never ran a
        Debug-plane op is a no-op.
        """
        uid = _as_uuid(session_id)
        if uid is None:
            return _config_error(session_id)
        with bind_context(principal=ctx.principal):
            provider_binding: ProviderBinding | None = None
            async with pool.connection() as conn:
                session = await DEBUG_SESSIONS.get(conn, uid)
                if session is None or session.project not in ctx.projects:
                    return _config_error(session_id)
                require_role(ctx, session.project, Role.OPERATOR)
                resolved = await _resolve_session_system(conn, uid)
                if resolved is None:
                    return _config_error(session_id)
                _, system_id = resolved
                if isinstance(self._connector_source, ProviderResolver):
                    try:
                        provider_binding = await self._connector_source.binding_for_session(
                            conn, uid
                        )
                    except CategorizedError as exc:
                        return ToolResponse.failure_from_error(session_id, exc)
                    connector = provider_binding.runtime.connector
                else:
                    connector = self._connector_source
                envelope = await _detach_locked(conn, ctx, uid, system_id, connector)
            if self._runtime is not None:
                runtime = self._runtime
                if isinstance(runtime, DebugRuntimeResolver):
                    if provider_binding is None:
                        raise RuntimeError("provider-aware debug runtime requires resolver")
                    runtime = runtime.runtime_for_binding(provider_binding)
                async with runtime.lock_for(session_id):
                    runtime.reap(session_id)
            self._secret_registry.release(_secret_scope(uid))
            return envelope


def _resolve_credential(
    system: System, transport: str, secret_backend: SecretBackend | None
) -> None | ToolResponse:
    """Resolve + register the ssh credential before transport use (ADR-0039 §2 ordering).

    Returns ``None`` when no credential is required (gdbstub) or resolution succeeded, or a
    failure envelope. The resolved value is registered into the redaction registry by the
    backend (a structural post-condition of ``FileRefBackend.resolve``) before this returns —
    so the connector that opens the SSH connection runs with the registry already seeded.
    """
    if transport != _SSH:
        return None
    try:
        profile = ProvisioningProfile.parse(system.provisioning_profile)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(str(system.id), exc)
    ref = ssh_credential_ref(profile)
    if ref is None:
        return _config_error(str(system.id), data={"reason": "ssh_credential_ref_missing"})
    if secret_backend is None:
        return ToolResponse.failure(str(system.id), ErrorCategory.MISSING_DEPENDENCY)
    try:
        secret_backend.resolve(ref)
    except PathSafetyError:
        # A reference that escapes the secrets root / points at a non-file is a caller-config
        # error for the file-backed secret backend.
        return ToolResponse.failure(str(system.id), ErrorCategory.CONFIGURATION_ERROR)
    except CategorizedError as exc:
        # Preserve the backend's own category (e.g. a manager backend's MISSING_DEPENDENCY /
        # INFRASTRUCTURE_FAILURE) so a degraded secret store is not mislabeled as bad input.
        return ToolResponse.failure_from_error(str(system.id), exc)
    return None


async def _attach_preconditions(
    conn: AsyncConnection, run: Run, transport: str
) -> System | ToolResponse:
    """Lockless pre-checks: Run booted, System present + `ready`, endpoint free.

    Returns the System on success, or a failure envelope. These are advisory fast-fails;
    `_insert_session_locked` re-checks conflict + ready authoritatively under the lock. The
    conflict check is scoped to ``transport`` (per-transport single-attach, ADR-0039 §4).
    """
    if run.state is not RunState.SUCCEEDED:
        return _config_error(str(run.id), data={"current_status": run.state.value})
    boot_result = await _succeeded_boot_result(conn, run.id)
    if boot_result is None:
        return _config_error(str(run.id), data={"reason": "boot_first"})
    if boot_result.get("boot_outcome") == "expected_crash_observed":
        return _config_error(str(run.id), data={"reason": "expected_crash_not_live_debuggable"})
    system = await _system_for_run(conn, run)
    if system is None:
        return _config_error(str(run.id))
    if system.state is not SystemState.READY:
        return _config_error(str(run.id), data={"current_status": system.state.value})
    if await _system_occupied(conn, system.id, transport):
        return ToolResponse.failure(str(run.id), ErrorCategory.TRANSPORT_CONFLICT)
    return system


async def _insert_session_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    system: System,
    handle: TransportHandle,
    connector: Connector,
    transport: str,
    session_id: UUID,
) -> ToolResponse:
    """Re-check conflict + ready under the per-System lock, then insert + drive `-> live`.

    A lost race (System crashed, or another attach committed first) closes the just-opened
    transport and returns the categorized error — no `live` row escapes the lock. The
    conflict re-check is scoped to ``transport`` (per-transport single-attach, ADR-0039 §4).
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system.id):
        current = await SYSTEMS.get(conn, system.id)
        if current is None or current.state is not SystemState.READY:
            await _close(connector, str(handle))
            status = current.state.value if current else "torn_down"
            return _config_error(str(run.id), data={"current_status": status})
        if await _system_occupied(conn, system.id, transport):
            await _close(connector, str(handle))
            return ToolResponse.failure(str(run.id), ErrorCategory.TRANSPORT_CONFLICT)
        now = datetime.now(UTC)  # placeholder; the DB owns created_at/updated_at
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=session_id,
                created_at=now,
                updated_at=now,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=run.project,
                run_id=run.id,
                state=DebugSessionState.ATTACH,
                transport=transport,
                transport_handle=str(handle),
                worker_heartbeat_at=now,
            ),
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="debug.start_session",
                object_kind="debug_sessions",
                object_id=session.id,
                transition="->attach",
                args={"run_id": str(run.id)},
                project=run.project,
            ),
        )
        await DEBUG_SESSIONS.update_state(conn, session.id, DebugSessionState.LIVE)
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="debug.start_session",
                object_kind="debug_sessions",
                object_id=session.id,
                transition="attach->live",
                args={"run_id": str(run.id)},
                project=run.project,
            ),
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
        await _close(connector, row["transport_handle"])
        await DEBUG_SESSIONS.update_state(conn, session_id, DebugSessionState.DETACHED)
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="debug.end_session",
                object_kind="debug_sessions",
                object_id=session_id,
                transition=f"{row['state']}->detached",
                args={"session_id": str(session_id)},
                project=row["project"],
            ),
        )
    return _detached_envelope(session_id, row["project"])


async def _close(connector: Connector, handle: str | None) -> None:
    """Close the transport best-effort; a missing/failing close never blocks the detach."""
    if handle is None:
        return
    with contextlib.suppress(CategorizedError):
        await asyncio.to_thread(connector.close_transport, TransportHandle(handle))


def _detached_envelope(session_id: UUID, project: str) -> ToolResponse:
    return ToolResponse.success(
        str(session_id), "detached", suggested_next_actions=[], data={"project": project}
    )


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver | None = None,
    secret_registry: SecretRegistry,
) -> None:
    """Register the `debug.*` tools on ``app``, bound to ``pool``.

    The connector and Debug-plane gdb-MI runtime are resolved from the owning provider at
    session/op time (no libvirt connection at registration — the resolver/prober are lazy
    `live_vm` seams). The Debug-plane gdb-MI tier (ADR-0034) caches one
    :class:`DebugEngineRuntime` per provider kind; its seven tools register here too, so
    `app.py` is untouched. `end_session` reaps the lazy engine via the same provider cache.
    """
    if resolver is None:
        raise RuntimeError("debug registrar requires an injected provider resolver")
    runtime = DebugRuntimeResolver(resolver)
    handlers = DebugSessionHandlers(
        resolver,
        runtime=runtime,
        secret_backend_factory=lambda session_id: secret_backend_from_env(
            registry=secret_registry, scope=_secret_scope(session_id)
        ),
        secret_registry=secret_registry,
    )

    @app.tool(
        name="debug.start_session",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def debug_start_session(
        run_id: Annotated[str, Field(description="The booted Run to attach a debug session to.")],
        transport: Annotated[
            str,
            Field(description="Transport kind: `gdbstub` (default) or `ssh`."),
        ] = _GDBSTUB,
    ) -> ToolResponse:
        """Open a single-attach transport and insert a live DebugSession. Requires operator."""
        return await handlers.start_session(
            pool, current_context(), run_id=run_id, transport=transport
        )

    @app.tool(
        name="debug.end_session",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def debug_end_session(
        session_id: Annotated[str, Field(description="The DebugSession to detach and close.")],
    ) -> ToolResponse:
        """Drive a live/attach DebugSession to detached; close its transport. Requires operator."""
        return await handlers.end_session(pool, current_context(), session_id)

    _register_debug_ops(app, pool, runtime)
