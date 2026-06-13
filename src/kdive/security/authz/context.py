"""Transport-neutral authenticated request context (ADR-0006, ADR-0020, ADR-0043)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from kdive.security.authz.errors import AuthError, ProjectMembershipDenied
from kdive.security.authz.rbac import (
    PlatformRole,
    Role,
    platform_roles_from_claims,
    roles_from_claims,
)


@dataclass(frozen=True)
class RequestContext:
    """The principal, session, project, and role grants attached to one request."""

    principal: str
    agent_session: str | None
    projects: tuple[str, ...]
    roles: Mapping[str, Role] = field(default_factory=dict, compare=False)
    platform_roles: frozenset[PlatformRole] = field(default_factory=frozenset, compare=False)
    client_id: str | None = field(default=None, compare=False)


def context_from_claims(claims: Mapping[str, object]) -> RequestContext:
    """Derive a request context from verified token claims.

    Raises:
        AuthError: The token carries no usable subject, or a malformed claim.
    """
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AuthError("verified token has no usable subject (sub) claim")
    agent_session = claims.get("agent_session")
    if agent_session is not None and not isinstance(agent_session, str):
        raise AuthError("agent_session claim is not a string")
    raw_projects = claims.get("projects")
    if raw_projects is None:
        raw_projects = ()
    if not isinstance(raw_projects, (list, tuple)):
        raise AuthError("projects claim is not a list")
    projects: list[str] = []
    for project in raw_projects:
        if not isinstance(project, str) or not project:
            raise AuthError("projects claim entries must be non-empty strings")
        projects.append(project)
    raw_client_id = claims.get("azp") or claims.get("client_id")
    client_id = raw_client_id if isinstance(raw_client_id, str) and raw_client_id else None
    return RequestContext(
        principal=subject,
        agent_session=agent_session,
        projects=tuple(projects),
        roles=roles_from_claims(claims),
        platform_roles=platform_roles_from_claims(claims),
        client_id=client_id,
    )


def require_project(ctx: RequestContext, project: str) -> str:
    """Validate that ``project`` is granted to ``ctx``; return it, or raise.

    Raises:
        ProjectMembershipDenied: ``project`` is not in ``ctx.projects``. The dispatch
            boundary catches this subclass and envelopes it as ``authorization_denied``
            (exit 3, ADR-0098); it stays an ``AuthError`` so existing catches still apply.
    """
    if project not in ctx.projects:
        raise ProjectMembershipDenied(f"project {project!r} is not granted to {ctx.principal!r}")
    return project
