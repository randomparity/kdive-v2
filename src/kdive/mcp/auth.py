"""Bearer-JWT verification and the request-context accessor (ADR-0010, ADR-0006).

`build_verifier` constructs FastMCP's `JWTVerifier` from the OIDC env vars; it
enforces `iss` and `aud` natively (ADR-0002). `context_from_claims` turns a verified
token's claims into the `(principal, agent_session, project)` tuple every tool reads
for attribution. `current_context` is the FastMCP-facing accessor; `require_project`
validates a requested project against the token's granted set (its first callers are
the plane tools, not `jobs.*`).
"""

from __future__ import annotations

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token

import kdive.config as config
from kdive.config.core_settings import OIDC_AUDIENCE, OIDC_ISSUER, OIDC_JWKS_URI
from kdive.config.registry import Setting
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.authz.context import RequestContext, context_from_claims, require_project
from kdive.security.authz.errors import AuthError, ProjectMembershipDenied

__all__ = [
    "AuthError",
    "ProjectMembershipDenied",
    "RequestContext",
    "build_verifier",
    "context_from_claims",
    "current_context",
    "require_project",
]


def _require_env(setting: Setting[str]) -> str:
    value = config.get(setting)
    if not value:
        raise CategorizedError(
            f"{setting.name} is not set; cannot verify bearer tokens",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return value


def build_verifier() -> JWTVerifier:
    """Build the `JWTVerifier` from the OIDC env vars, enforcing `iss` + `aud`."""
    return JWTVerifier(
        jwks_uri=_require_env(OIDC_JWKS_URI),
        issuer=_require_env(OIDC_ISSUER),
        audience=_require_env(OIDC_AUDIENCE),
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
