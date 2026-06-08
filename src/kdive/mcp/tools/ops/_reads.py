"""Shared helpers for the cross-project ``platform_auditor`` reads (ADR-0062 §6).

``audit.query`` (cross-project form) and ``inventory.list`` both gate on
``require_platform_role(PLATFORM_AUDITOR)`` and read-audit to ``platform_audit_log`` —
never to the per-project ``audit_log`` they may inspect. The success-audit and the
SoD-denial-audit shapes are identical across both, so they live here once:

* :func:`parse_window` — the ``[start, end]`` ISO-8601 ``timestamptz`` filter, fail-closed
  on a malformed/naive/inverted bound (mirrors ``accounting.reports``).
* :func:`record_read` — one ``platform_audit_log`` row for a served cross-project read.
* :func:`audit_denial` — a SoD-denial row, written **iff** the caller holds ≥1 platform
  role (ADR-0043 §4: a project-only token's denial is the routine non-grant case and is
  not recorded, so an openly-callable read cannot be amplified into unbounded writes).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools._time_window import parse_timestamptz_window
from kdive.mcp.tools.ops._auth import (
    ALL_PROJECTS_SCOPE,
    audit_platform_denial,
    held_platform_roles,
)
from kdive.security import audit

if TYPE_CHECKING:
    from kdive.security.authz.context import RequestContext


def parse_window(window: object) -> tuple[datetime | None, datetime | None] | None:
    """Parse ``window`` into a ``(start, end)`` datetime pair, or ``None`` for all time.

    ``window`` is a two-element ``[start, end]`` of **timezone-aware** ISO-8601 strings
    (either may be ``None``), or ``None``. Fails closed (``configuration_error``) on a
    non-pair, an unparseable or tz-naive bound, or a non-ordered ``start >= end`` range,
    so a malformed filter surfaces an error rather than a silently-empty result.
    ``audit_log.ts`` is ``timestamptz``; a tz-naive bound would compare in an unintended
    zone.
    """
    return parse_timestamptz_window(window, timestamp_column="audit_log.ts")


async def record_read(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    tool: str,
    args: dict[str, object],
) -> None:
    """Write one ``platform_audit_log`` row for a served cross-project read.

    Runs on the **same** connection that performed the read, in a nested transaction, so
    the read-access row commits atomically with serving the read (mirrors
    ``accounting.reports._report_all_projects``): an auditor cannot see cross-tenant data
    without leaving the audit row behind.
    """
    async with conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=tool,
                scope=ALL_PROJECTS_SCOPE,
                args=args,
                platform_role=held_platform_roles(ctx),
            ),
        )


async def audit_denial(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    args: dict[str, object],
) -> None:
    """Audit a cross-project read denial iff the caller holds ≥1 platform role (ADR-0043 §4).

    A project-only token's denial is the routine non-grant case and is *not* recorded —
    auditing it would let any authenticated token amplify writes into ``platform_audit_log``
    on this openly-callable read. The role check runs before any pool connection is open, so
    the denial-audit opens its own connection and transaction here.
    """
    await audit_platform_denial(pool, ctx, tool=tool, scope=ALL_PROJECTS_SCOPE, args=args)
