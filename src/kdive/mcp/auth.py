"""Bearer-JWT verification and the request-context accessor (ADR-0010, ADR-0006).

`build_verifier` constructs FastMCP's `JWTVerifier` from the OIDC env vars; it
enforces `iss` and `aud` natively (ADR-0002). `context_from_claims` turns a verified
token's claims into the `(principal, agent_session, project)` tuple every tool reads
for attribution. `current_context` is the FastMCP-facing accessor; `require_project`
validates a requested project against the token's granted set (its first callers are
the plane tools, not `jobs.*`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.rbac import Role, roles_from_claims

_JWKS_URI_ENV = "KDIVE_OIDC_JWKS_URI"
_ISSUER_ENV = "KDIVE_OIDC_ISSUER"
_AUDIENCE_ENV = "KDIVE_OIDC_AUDIENCE"


class AuthError(Exception):
    """A verified transport carried claims that cannot authorize the request.

    Distinct from transport-level rejection (a missing/invalid/expired bearer is a
    401 from FastMCP's middleware before any tool runs). Raised when the verified
    token lacks a usable subject, or a requested project is not granted.
    """


@dataclass(frozen=True)
class RequestContext:
    """The `(principal, agent_session, project)` attribution tuple (ADR-0006).

    ``roles`` carries the principal's per-project role (ADR-0020). It is excluded from
    ``__eq__``/``__hash__`` (``compare=False``) so a ``dict`` field cannot make the
    frozen dataclass unhashable; the annotation is a string at runtime
    (``from __future__ import annotations``), so ``Role`` is only a typing dependency.
    """

    principal: str
    agent_session: str | None
    projects: tuple[str, ...]
    roles: Mapping[str, Role] = field(default_factory=dict, compare=False)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CategorizedError(
            f"{name} is not set; cannot verify bearer tokens",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return value


def build_verifier() -> JWTVerifier:
    """Build the `JWTVerifier` from the OIDC env vars, enforcing `iss` + `aud`."""
    return JWTVerifier(
        jwks_uri=_require_env(_JWKS_URI_ENV),
        issuer=_require_env(_ISSUER_ENV),
        audience=_require_env(_AUDIENCE_ENV),
    )


def context_from_claims(claims: Mapping[str, object]) -> RequestContext:
    """Derive the request context from a verified token's claims.

    Reads the principal from ``claims["sub"]`` (FastMCP leaves
    ``AccessToken.subject`` unset). ``agent_session`` is optional in M0; ``projects``
    defaults to an empty tuple.

    Raises:
        AuthError: The token carries no usable ``sub``, or a malformed
            ``agent_session`` / ``projects`` claim.
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
        # A non-list `projects` (e.g. `0`, `""`, a string, an object) is malformed.
        # Reject it rather than letting a falsy value silently coerce to "no projects"
        # — matches the `agent_session` check above and this function's documented
        # contract (fail closed on a malformed claim, never silently grant-nothing).
        raise AuthError("projects claim is not a list")
    projects = tuple(str(p) for p in raw_projects)
    return RequestContext(
        principal=subject,
        agent_session=agent_session,
        projects=projects,
        roles=roles_from_claims(claims),
    )


def current_context() -> RequestContext:
    """Read the context from the in-flight request's verified token.

    Raises:
        AuthError: No verified token reached the tool (defense in depth; the auth
            middleware should already have returned 401).
    """
    token = get_access_token()
    if token is None:
        raise AuthError("no authenticated token in the request context")
    return context_from_claims(token.claims)


def require_project(ctx: RequestContext, project: str) -> str:
    """Validate ``project`` is granted to ``ctx``; return it, or raise.

    Raises:
        AuthError: ``project`` is not in the token's ``projects`` claim.
    """
    if project not in ctx.projects:
        raise AuthError(f"project {project!r} is not granted to {ctx.principal!r}")
    return project
