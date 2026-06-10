"""Append-only audit records for state transitions (ADR-0006, ADR-0020).

`record` writes exactly one `audit_log` row inside the caller's transaction, so a
state transition and its audit entry commit atomically. `args_digest` stores a
one-way SHA-256 of the tool arguments — never the raw values — so secret-bearing
arguments cannot leak as plaintext (confidentiality of low-entropy secret *values* is
ADR-0012's secrets-by-reference contract; the digest is tamper-evidence/correlation).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from kdive.security.authz.errors import AuthError

if TYPE_CHECKING:
    from psycopg import AsyncConnection

    from kdive.security.authz.context import RequestContext


def args_digest(args: Mapping[str, object]) -> str:
    """Return the SHA-256 hex of a canonical JSON encoding of ``args``.

    ``args`` are JSON-native values (MCP tool arguments) plus the scalar ``UUID`` /
    ``datetime`` the codebase carries, which ``default=str`` renders deterministically.
    The digest is one-way: no plaintext argument is stored.
    """
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """A project-scoped audited transition."""

    tool: str
    object_kind: str
    object_id: UUID
    transition: str
    args: Mapping[str, object]
    project: str


@dataclass(frozen=True, slots=True)
class DenialEvent:
    """A member-over-reach `require_role` denial (ADR-0062 §5).

    Object-agnostic: the dispatch boundary records the actor, tool, and project (the last
    taken from the :class:`~kdive.security.authz.rbac.RoleDenied` exception), not the object the
    handler would have resolved after the gate. ``reason`` is the human-readable denial
    text (e.g. the held-vs-required role); it carries no secret values.
    """

    principal: str
    agent_session: str | None
    project: str
    tool: str
    args: Mapping[str, object]
    reason: str


@dataclass(frozen=True, slots=True)
class PlatformAuditEvent:
    """A platform-scope read or denial audit event.

    ``actor`` is the required caller classification (operator-cli | agent | unknown)
    resolved from the OIDC client id (ADR-0089). It has no default so every construction
    site must attribute the caller — an unported site fails to construct.
    """

    tool: str
    scope: str
    args: Mapping[str, object]
    platform_role: str | None
    actor: str


async def record(
    conn: AsyncConnection,
    ctx: RequestContext,
    event: AuditEvent,
) -> UUID:
    """Append one `audit_log` row for a transition; return its id.

    Runs the INSERT on ``conn`` without opening a transaction, so the caller composes
    it with the audited state transition in one ``conn.transaction()`` (both commit or
    neither does). ``project`` is the audited object's project, not ``ctx.projects``
    (the granted set).

    Raises:
        AuthError: ``project`` is not in ``ctx.projects`` — a misattribution guard on
            the append-only row.
    """
    if event.project not in ctx.projects:
        raise AuthError(
            f"cannot audit under project {event.project!r} not granted to {ctx.principal!r}"
        )
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO audit_log "
            "(principal, agent_session, project, tool, object_kind, object_id, "
            " transition, args_digest) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (
                ctx.principal,
                ctx.agent_session,
                event.project,
                event.tool,
                event.object_kind,
                event.object_id,
                event.transition,
                args_digest(event.args),
            ),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
        raise RuntimeError("INSERT into audit_log returned no row")
    return row[0]


async def record_platform(
    conn: AsyncConnection,
    *,
    principal: str,
    agent_session: str | None,
    event: PlatformAuditEvent,
) -> UUID:
    """Append one `platform_audit_log` row for a platform read/denial; return its id.

    Platform authority is project-independent (ADR-0043 §4), so — unlike :func:`record` —
    there is **no** ``project in ctx.projects`` guard: a principal with empty
    ``ctx.projects`` (a platform-only token) writes a row. Used for successful platform
    reads, audited granted-set member reads (``platform_role=None``), and
    ``require_platform_role`` denials.

    ``scope`` describes the breadth of the read (e.g. ``"all-projects"`` or the project
    set), not a single project/object. Runs the INSERT on ``conn`` without opening a
    transaction, so the caller composes it (a denial-audit, for instance, runs in its own
    connection in the tool's ``except`` path).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO platform_audit_log "
            "(principal, agent_session, platform_role, tool, scope, args_digest, actor) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (
                principal,
                agent_session,
                event.platform_role,
                event.tool,
                event.scope,
                args_digest(event.args),
                event.actor,
            ),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
        raise RuntimeError("INSERT into platform_audit_log returned no row")
    return row[0]


_DENIED_TRANSITION = "denied"


async def record_denial(
    conn: AsyncConnection,
    *,
    event: DenialEvent,
) -> UUID:
    """Append one `audit_log` row for a member-over-reach denial; return its id.

    The guard-exempt denial writer (precedent: :func:`record_system` — no
    ``project in ctx.projects`` membership guard, since the dispatch boundary has already
    authenticated the actor and the project comes from the ``RoleDenied`` exception). It
    writes the **reserved bare** ``transition='denied'`` literal with NULL object columns
    (the boundary is object-agnostic) — distinct from the destructive gate's
    ``f"{op.kind}:denied"`` convention, whose rows always carry their gated object. The
    ``tool`` column records which tool was denied, so the bare transition loses nothing.

    Runs the INSERT on ``conn`` without opening a transaction, so the caller composes it
    (the dispatch boundary opens its own connection in the denial path).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO audit_log "
            "(principal, agent_session, project, tool, object_kind, object_id, "
            " transition, args_digest, reason) "
            "VALUES (%s, %s, %s, %s, NULL, NULL, %s, %s, %s) "
            "RETURNING id",
            (
                event.principal,
                event.agent_session,
                event.project,
                event.tool,
                _DENIED_TRANSITION,
                args_digest(event.args),
                event.reason,
            ),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
        raise RuntimeError("INSERT into audit_log returned no row")
    return row[0]


async def record_system(
    conn: AsyncConnection,
    *,
    principal: str,
    event: AuditEvent,
    agent_session: str | None = None,
) -> UUID:
    """Append one `audit_log` row for a system-initiated transition (no RequestContext).

    The reconciler acts cross-project on the platform's behalf (ADR-0021), so it has no
    principal-scoped :class:`RequestContext` and the membership guard :func:`record`
    applies does not fit. This writes the row under an explicit system ``principal`` (e.g.
    ``system:reconciler``) and ``project`` (the audited object's). Runs on ``conn`` without
    opening a transaction, so the caller composes it with the audited transition in one
    ``conn.transaction()``.

    ``agent_session`` defaults to ``None`` (a reconciler teardown carries no session). The
    promotion sweep (ADR-0069 §4) passes the queued allocation's **original**
    ``(principal, agent_session)`` so a backlog grant is indistinguishable in audit from a
    synchronous one, even though the sweep itself runs under the service identity.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO audit_log "
            "(principal, agent_session, project, tool, object_kind, object_id, "
            " transition, args_digest) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (
                principal,
                agent_session,
                event.project,
                event.tool,
                event.object_kind,
                event.object_id,
                event.transition,
                args_digest(event.args),
            ),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
        raise RuntimeError("INSERT into audit_log returned no row")
    return row[0]
