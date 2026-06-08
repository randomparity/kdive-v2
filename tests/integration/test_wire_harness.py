"""Three-tier wire smoke test (ADR-0044): in-memory / oidc_issuer / live_stack.

The in-memory tier (Docker-free) covers the claim shape; the ``oidc_issuer`` tier is the
standing claim-shape gate (real issuer + real verifier); the ``live_stack`` tier drives the
per-role ``resources.list`` probe over real HTTP — the only tier where authenticated tool
dispatch works (the in-memory transport carries no token).
"""

from __future__ import annotations

import asyncio

import jwt  # PyJWT: decode a token's claims without verifying the signature
import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier

from kdive.security.authz.rbac import Role, roles_from_claims
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.harness import LiveStackClient, _build_claims, mint_token
from tests.mcp.conftest import AUDIENCE, make_keypair, mint

_PROJECT = "proj-a"
# (subject, roles map, platform_roles) per role token the smoke exercises.
_ROLE_SUBJECTS = (
    ("viewer-proj-a", {_PROJECT: "viewer"}, None),
    ("operator-proj-a", {_PROJECT: "operator"}, None),
    ("admin-proj-a", {_PROJECT: "admin"}, None),
    ("auditor", {_PROJECT: "viewer"}, ["platform_auditor"]),
)


def test_inmemory_tier_claim_shapes_round_trip() -> None:
    """_build_claims + the in-process mint produce the nested roles + platform_roles shapes."""
    keypair = make_keypair()
    token = mint(keypair, subject="auditor", projects=[_PROJECT], roles={_PROJECT: "viewer"})
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["roles"] == {_PROJECT: "viewer"}  # nested object survives the JWT

    claims = _build_claims(
        subject="auditor",
        audience=AUDIENCE,
        projects=[_PROJECT],
        roles={_PROJECT: "viewer"},
        platform_roles=["platform_auditor"],
        agent_session=None,
    )
    assert claims["platform_roles"] == ["platform_auditor"]  # flat array
    assert claims["roles"] == {_PROJECT: "viewer"}  # nested object


@pytest.mark.oidc_issuer
def test_oidc_issuer_tier_mints_and_verifies_claim_shapes() -> None:
    """The gate (ADR-0044): the issuer mints nested roles + platform_roles into the access
    token; the real JWTVerifier accepts; roles_from_claims parses; wrong-aud rejects."""
    issuer = require_issuer()

    async def _run() -> None:
        verifier = JWTVerifier(
            jwks_uri=issuer.jwks_uri, issuer=issuer.base_url, audience=issuer.audience
        )
        wrong_aud = JWTVerifier(
            jwks_uri=issuer.jwks_uri, issuer=issuer.base_url, audience="not-kdive"
        )
        for subject, roles, platform_roles in _ROLE_SUBJECTS:
            token = mint_token(
                issuer,
                subject=subject,
                projects=[_PROJECT],
                roles=roles,
                platform_roles=platform_roles,
                agent_session="sess-1",
            )
            verified = await verifier.verify_token(token)
            assert verified is not None, f"real verifier rejected {subject}'s token"
            assert verified.claims["roles"] == roles  # nested object survived
            parsed = roles_from_claims(verified.claims)
            assert parsed == {p: Role(r) for p, r in roles.items()}
            if platform_roles is not None:
                assert verified.claims["platform_roles"] == platform_roles  # flat array
            assert await wrong_aud.verify_token(token) is None  # verifier enforces aud

    asyncio.run(_run())


@pytest.mark.live_stack
def test_live_stack_tier_reads_resources_over_http_per_role() -> None:
    """Over HTTP against a host-run server: list_tools + a resources.list per role, tokens
    minted by the real issuer and validated through the server's verifier."""
    issuer = require_issuer()
    base_url = require_stack()

    async def _run() -> None:
        for subject, roles, platform_roles in _ROLE_SUBJECTS:
            token = mint_token(
                issuer,
                subject=subject,
                projects=[_PROJECT],
                roles=roles,
                platform_roles=platform_roles,
                agent_session="sess-1",
            )
            client = LiveStackClient.over_http(base_url, token)
            async with client:
                names = await client.list_tools()
                assert "resources.list" in names
                result = await client.call_tool("resources.list")
            assert isinstance(result, list)

    asyncio.run(_run())
