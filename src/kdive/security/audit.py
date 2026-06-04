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
from typing import TYPE_CHECKING
from uuid import UUID

from kdive.mcp.auth import AuthError

if TYPE_CHECKING:
    from psycopg import AsyncConnection

    from kdive.mcp.auth import RequestContext


def args_digest(args: Mapping[str, object]) -> str:
    """Return the SHA-256 hex of a canonical JSON encoding of ``args``.

    ``args`` are JSON-native values (MCP tool arguments) plus the scalar ``UUID`` /
    ``datetime`` the codebase carries, which ``default=str`` renders deterministically.
    The digest is one-way: no plaintext argument is stored.
    """
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def record(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    tool: str,
    object_kind: str,
    object_id: UUID,
    transition: str,
    args: Mapping[str, object],
    project: str,
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
    if project not in ctx.projects:
        raise AuthError(f"cannot audit under project {project!r} not granted to {ctx.principal!r}")
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
                project,
                tool,
                object_kind,
                object_id,
                transition,
                args_digest(args),
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
    tool: str,
    object_kind: str,
    object_id: UUID,
    transition: str,
    args: Mapping[str, object],
    project: str,
) -> UUID:
    """Append one `audit_log` row for a system-initiated transition (no RequestContext).

    The reconciler acts cross-project on the platform's behalf (ADR-0021), so it has no
    principal-scoped :class:`RequestContext` and the membership guard :func:`record`
    applies does not fit. This writes the row under an explicit system ``principal`` (e.g.
    ``system:reconciler``) and ``project`` (the audited object's), with no
    ``agent_session``. Runs on ``conn`` without opening a transaction, so the caller
    composes it with the audited transition in one ``conn.transaction()``.
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
                None,
                project,
                tool,
                object_kind,
                object_id,
                transition,
                args_digest(args),
            ),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
        raise RuntimeError("INSERT into audit_log returned no row")
    return row[0]
