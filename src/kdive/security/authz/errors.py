"""Shared authz exception types."""

from __future__ import annotations


class AuthError(Exception):
    """A verified transport carried claims that cannot authorize the request."""


class ProjectMembershipDenied(AuthError):
    """The caller named a project they are not a member of (ADR-0098).

    Subclasses :class:`AuthError` so existing membership semantics and broad ``AuthError``
    catches are preserved unchanged. The MCP dispatch boundary
    (:class:`~kdive.mcp.middleware.DenialAuditMiddleware`) catches this *subclass*
    specifically to envelope it as ``authorization_denied`` (exit 3), while a bare
    :class:`AuthError` (an authentication failure — no subject, no token) keeps raising. The
    denial is **not** audited: it is the non-member case, excluded to avoid write-amplification
    on openly-callable reads (ADR-0043 §4).
    """
