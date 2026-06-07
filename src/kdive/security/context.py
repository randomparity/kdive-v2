"""Transport-neutral authenticated request context (ADR-0006, ADR-0020, ADR-0043)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from kdive.security.rbac import (
    PlatformRole,
    Role,
    platform_roles_from_claims,
    roles_from_claims,
)


class AuthError(Exception):
    """A verified transport carried claims that cannot authorize the request."""


@dataclass(frozen=True)
class RequestContext:
    """The principal, session, project, and role grants attached to one request."""

    principal: str
    agent_session: str | None
    projects: tuple[str, ...]
    roles: Mapping[str, Role] = field(default_factory=dict, compare=False)
    platform_roles: frozenset[PlatformRole] = field(default_factory=frozenset, compare=False)


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
    projects = tuple(str(p) for p in raw_projects)
    return RequestContext(
        principal=subject,
        agent_session=agent_session,
        projects=projects,
        roles=roles_from_claims(claims),
        platform_roles=platform_roles_from_claims(claims),
    )


def require_project(ctx: RequestContext, project: str) -> str:
    """Validate that ``project`` is granted to ``ctx``; return it, or raise."""
    if project not in ctx.projects:
        raise AuthError(f"project {project!r} is not granted to {ctx.principal!r}")
    return project
