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
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, LiteralString, cast
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
from kdive.mcp.tools.debug.session_context import resolve_debug_session_context
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.ports import (
    DEBUG_TRANSPORT_KINDS,
    Connector,
    DebugTransportKind,
    SystemHandle,
    TransportHandle,
)
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProfilePolicy
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.security.secrets.paths import PathSafetyError
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_GDBSTUB = "gdbstub"
_DRGN_LIVE = "drgn-live"
_log = logging.getLogger(__name__)
# An attach failure maps these provider categories onto the response envelope. A
# MISSING_DEPENDENCY (no live_vm host / unresolvable endpoint) surfaces as an attach failure:
# the agent cannot attach either way.
_ATTACH_FAILURE = frozenset({ErrorCategory.DEBUG_ATTACH_FAILURE, ErrorCategory.TRANSPORT_FAILURE})

# A live/attach session occupies the System's single endpoint **for that transport kind**
# (single-attach per transport, ADR-0039 §4): a gdbstub and a drgn-live session may coexist on
# one System, but a second attach over the same transport is `transport_conflict`.
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
    transport: DebugTransportKind
    connector: Connector


@dataclass(frozen=True)
class _DetachResources:
    connector: Connector
    runtime: DebugEngineRuntime | None = None


@dataclass(frozen=True)
class _AttachResources:
    connector: Connector
    profile_policy: ProfilePolicy


type _ConnectorForRun = Callable[[AsyncConnection, Run], Awaitable[_AttachResources | ToolResponse]]
type _DetachResourcesForSession = Callable[
    [AsyncConnection, UUID], Awaitable[_DetachResources | ToolResponse]
]


def _resolved_connector_for_run(resolver: ProviderResolver) -> _ConnectorForRun:
    async def connector_for_run(conn: AsyncConnection, run: Run) -> _AttachResources | ToolResponse:
        try:
            runtime = await resolver.runtime_for_run(conn, run.id)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(str(run.id), exc)
        return _AttachResources(connector=runtime.connector, profile_policy=runtime.profile_policy)

    return connector_for_run


def _resolved_detach_resources(
    resolver: ProviderResolver, runtime_resolver: DebugRuntimeResolver | None
) -> _DetachResourcesForSession:
    async def detach_resources(
        conn: AsyncConnection, session_id: UUID
    ) -> _DetachResources | ToolResponse:
        try:
            binding = await resolver.binding_for_session(conn, session_id)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(str(session_id), exc)
        runtime: DebugEngineRuntime | None
        if runtime_resolver is None:
            runtime = None
        else:
            resolved_runtime = runtime_resolver.runtime_for_binding(
                binding, object_id=str(session_id)
            )
            if isinstance(resolved_runtime, ToolResponse):
                return resolved_runtime
            runtime = resolved_runtime
        return _DetachResources(connector=binding.runtime.connector, runtime=runtime)

    return detach_resources


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


async def _system_occupied(
    conn: AsyncConnection, system_id: UUID, transport: DebugTransportKind
) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(_OCCUPIED_SQL, (system_id, transport))
        return await cur.fetchone() is not None


async def _open_transport(
    connector: Connector, system: System, transport: DebugTransportKind
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
        *,
        connector_for_run: _ConnectorForRun,
        detach_resources: _DetachResourcesForSession,
        secret_backend_factory: Callable[[UUID], SecretBackend] | None = None,
        secret_registry: SecretRegistry,
    ) -> None:
        self._connector_for_run = connector_for_run
        self._detach_resources = detach_resources
        self._secret_backend_factory = secret_backend_factory
        self._secret_registry = secret_registry

    @classmethod
    def from_resolver(
        cls,
        resolver: ProviderResolver,
        *,
        runtime_resolver: DebugRuntimeResolver | None,
        secret_backend_factory: Callable[[UUID], SecretBackend] | None = None,
        secret_registry: SecretRegistry,
    ) -> DebugSessionHandlers:
        return cls(
            connector_for_run=_resolved_connector_for_run(resolver),
            detach_resources=_resolved_detach_resources(resolver, runtime_resolver),
            secret_backend_factory=secret_backend_factory,
            secret_registry=secret_registry,
        )

    async def start_session(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        run_id: str,
        transport: str = _GDBSTUB,
    ) -> ToolResponse:
        """Open a single-attach transport and insert a `live` DebugSession (operator).

        For a ``transport="drgn-live"`` session whose profile realizes it over SSH (the
        local-libvirt section; ``drgn_live_requires_credential``) the guest credential is
        resolved from the profile's ``ssh_credential_ref`` through the bound secret backend
        factory **before** the transport is opened, so the redaction registry is seeded before
        any transport output can carry the value (ADR-0039 §2). gdbstub, and a guest-agent
        drgn-live realization (remote), need no credential.
        """
        uid = _as_uuid(run_id)
        if uid is None:
            return _config_error(run_id)
        if transport not in DEBUG_TRANSPORT_KINDS:
            return _config_error(run_id)
        transport_kind = cast(DebugTransportKind, transport)
        session_id = uuid4()
        secret_scope = _secret_scope(session_id)
        with bind_context(principal=ctx.principal):
            async with pool.connection() as conn:
                request = await self._prepare_attach_request(
                    conn, ctx, uid, transport_kind, session_id
                )
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
        transport: DebugTransportKind,
        session_id: UUID,
    ) -> _AttachRequest | ToolResponse:
        run = await RUNS.get(conn, run_id)
        if run is None or run.project not in ctx.projects:
            return _config_error(str(run_id))
        require_role(ctx, run.project, Role.OPERATOR)
        system = await _attach_preconditions(conn, run, transport)
        if isinstance(system, ToolResponse):
            return system
        resources = await self._connector_for_run(conn, run)
        if isinstance(resources, ToolResponse):
            return resources
        backend = self._credential_backend(session_id, transport)
        resolved = _resolve_credential(system, transport, resources.profile_policy, backend)
        if isinstance(resolved, ToolResponse):
            return resolved
        return _AttachRequest(
            run=run,
            system=system,
            session_id=session_id,
            transport=transport,
            connector=resources.connector,
        )

    def _credential_backend(
        self, session_id: UUID, transport: DebugTransportKind
    ) -> SecretBackend | None:
        if transport != _DRGN_LIVE or self._secret_backend_factory is None:
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
            resources: _DetachResources
            async with pool.connection() as conn:
                resolved_session = await resolve_debug_session_context(
                    conn, ctx, session_id, include_system=True
                )
                if isinstance(resolved_session, ToolResponse):
                    return resolved_session
                if resolved_session.system_id is None:
                    return _config_error(session_id)
                resources_or_response = await self._detach_resources(conn, uid)
                if isinstance(resources_or_response, ToolResponse):
                    return resources_or_response
                resources = resources_or_response
                envelope = await _detach_locked(
                    conn, ctx, uid, resolved_session.system_id, resources.connector
                )
            if resources.runtime is not None:
                async with resources.runtime.lock_for(session_id):
                    resources.runtime.reap(session_id)
            self._secret_registry.release(_secret_scope(uid))
            return envelope


def _resolve_credential(
    system: System,
    transport: DebugTransportKind,
    profile_policy: ProfilePolicy,
    secret_backend: SecretBackend | None,
) -> None | ToolResponse:
    """Resolve + register the SSH credential before transport use (ADR-0039 §2 ordering).

    A credential is needed only for a ``drgn-live`` transport whose profile realizes it over
    SSH — the local-libvirt section, per ``ProfilePolicy.drgn_live_requires_credential``
    (ADR-0085 Decision 2). Returns ``None`` when none is required (gdbstub, or a guest-agent
    realization such as
    remote) or resolution succeeded, or a failure envelope. The resolved value is registered
    into the redaction registry by the backend (a structural post-condition of
    ``FileRefBackend.resolve``) before this returns — so the connector that opens the SSH
    connection runs with the registry already seeded.
    """
    if transport != _DRGN_LIVE:
        return None
    try:
        profile = ProvisioningProfile.parse(system.provisioning_profile)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(str(system.id), exc)
    if not profile_policy.drgn_live_requires_credential(profile):
        return None
    ref = profile_policy.ssh_credential_ref(profile)
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
    conn: AsyncConnection, run: Run, transport: DebugTransportKind
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
    transport: DebugTransportKind,
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
    try:
        await asyncio.to_thread(connector.close_transport, TransportHandle(handle))
    except Exception:
        _log.warning(
            "debug transport close failed; continuing detach",
            extra={"handle": handle},
            exc_info=True,
        )


def _detached_envelope(session_id: UUID, project: str) -> ToolResponse:
    return ToolResponse.success(
        str(session_id), "detached", suggested_next_actions=[], data={"project": project}
    )


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Register the `debug.*` tools on ``app``, bound to ``pool``.

    The connector and Debug-plane gdb-MI runtime are resolved from the owning provider at
    session/op time (no libvirt connection at registration — the resolver/prober are lazy
    `live_vm` seams). The Debug-plane gdb-MI tier (ADR-0034) caches one
    :class:`DebugEngineRuntime` per provider kind; its seven tools register here too, so
    `app.py` is untouched. `end_session` reaps the lazy engine via the same provider cache.
    """
    runtime = DebugRuntimeResolver(resolver)
    handlers = DebugSessionHandlers.from_resolver(
        resolver,
        runtime_resolver=runtime,
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
            Field(description="Transport kind: `gdbstub` (default) or `drgn-live`."),
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
