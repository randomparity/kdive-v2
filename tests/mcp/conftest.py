"""MCP test fixtures: re-export DB fixtures and a JWT-minting helper."""

from __future__ import annotations

from fastmcp.server.auth.providers.jwt import RSAKeyPair

# Re-export the disposable-Postgres fixtures so DB-backed MCP tests can use them.
from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401

ISSUER = "https://idp.test.kdive"
AUDIENCE = "kdive"


def make_keypair() -> RSAKeyPair:
    return RSAKeyPair.generate()


def mint(
    keypair: RSAKeyPair,
    *,
    subject: str = "user-1",
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    agent_session: str | None = "sess-1",
    projects: list[str] | None = None,
    roles: dict[str, str] | None = None,
    expires_in_seconds: int = 3600,
) -> str:
    """Mint a signed JWT carrying the kdive custom claims.

    ``roles`` is the per-project role map (``{"proj-a": "admin"}``) the
    ``roles_from_claims`` parser reads; omit it for a membership-only token.
    """
    extra: dict[str, object] = {}
    if agent_session is not None:
        extra["agent_session"] = agent_session
    if projects is not None:
        extra["projects"] = projects
    if roles is not None:
        extra["roles"] = roles
    return keypair.create_token(
        subject=subject,
        issuer=issuer,
        audience=audience,
        additional_claims=extra,
        expires_in_seconds=expires_in_seconds,
    )
