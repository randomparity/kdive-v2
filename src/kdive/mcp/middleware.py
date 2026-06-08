"""MCP dispatch-boundary middleware: the denial-audit retrofit (ADR-0062 §5, issue #142).

`require_role`'s **member-over-reach** site raises :class:`~kdive.security.rbac.RoleDenied`
(the dedicated discriminator, not the base :class:`~kdive.security.rbac.AuthorizationError`
the non-member site keeps). :class:`DenialAuditMiddleware` is the single tool-dispatch
boundary that catches **`RoleDenied` specifically**, writes one guard-exempt `audit_log`
denial row (object NULL, reserved bare ``transition='denied'``, ``project`` from the
exception), and returns the uniform authorization-denied envelope. Catching the
``AuthorizationError`` base instead would double-write
``require_platform_role`` denials and :class:`~kdive.security.gate.DestructiveOpDenied`
(both already handled elsewhere); the non-member denial is also deliberately excluded to
avoid write-amplification (ADR-0043 §4 / ADR-0062 §5).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware

from kdive.domain.errors import ErrorCategory
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.rbac import RoleDenied

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_log = logging.getLogger(__name__)


def _current_agent_session() -> str | None:
    """Read the in-flight request's ``agent_session`` from the verified token."""
    return current_context().agent_session


class DenialAuditMiddleware(Middleware):
    """Catch member-over-reach `RoleDenied` at the dispatch boundary and audit it.

    Args:
        pool: The shared async connection pool the denial row is written through (its own
            connection — the denial path runs after the tool's transaction has unwound).
        agent_session: A callable returning the in-flight ``agent_session`` (injected so
            the recording logic is unit-testable without a live request scope); defaults
            to reading it from the verified token via :func:`current_context`.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        agent_session: Callable[[], str | None] = _current_agent_session,
    ) -> None:
        self._pool = pool
        self._agent_session = agent_session

    async def on_call_tool(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Any:
        """Dispatch one tool call; audit and map a member-over-reach denial.

        Only :class:`RoleDenied` is caught — every other exception (including the base
        :class:`~kdive.security.rbac.AuthorizationError` non-member denial,
        :class:`~kdive.security.gate.DestructiveOpDenied`, and unrelated errors) propagates
        unaudited.
        """
        try:
            return await call_next(context)
        except RoleDenied as denial:
            tool = context.message.name
            try:
                await self._record(tool, denial)
            except Exception:
                _log.warning("failed to audit RoleDenied for tool %s", tool, exc_info=True)
            return ToolResponse.failure(tool, ErrorCategory.AUTHORIZATION_DENIED)

    async def _record(self, tool: str, denial: RoleDenied) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await audit.record_denial(
                conn,
                event=audit.DenialEvent(
                    principal=denial.principal,
                    agent_session=self._agent_session(),
                    project=denial.project,
                    tool=tool,
                    args={},
                    reason=str(denial),
                ),
            )
