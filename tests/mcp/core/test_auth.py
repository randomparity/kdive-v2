"""auth.py: verifier enforcement + context derivation."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier

from kdive.domain.errors import CategorizedError
from kdive.mcp.auth import (
    AuthError,
    RequestContext,
    build_verifier,
    context_from_claims,
    require_project,
)
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair, mint


def test_verifier_accepts_valid_and_rejects_iss_aud_expiry() -> None:
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)

    async def _run() -> None:
        good = await verifier.verify_token(mint(kp))
        assert good is not None
        assert good.claims["sub"] == "user-1"
        # subject is NOT populated by JWTVerifier — must read claims["sub"].
        assert good.subject is None
        assert await verifier.verify_token(mint(kp, issuer="https://evil")) is None
        assert await verifier.verify_token(mint(kp, audience="other")) is None
        assert await verifier.verify_token(mint(kp, expires_in_seconds=-10)) is None

    asyncio.run(_run())


def test_context_from_claims_full() -> None:
    ctx = context_from_claims({"sub": "user-9", "agent_session": "sess-x", "projects": ["a", "b"]})
    assert ctx == RequestContext(principal="user-9", agent_session="sess-x", projects=("a", "b"))


def test_context_from_claims_optional_fields_absent() -> None:
    ctx = context_from_claims({"sub": "user-9"})
    assert ctx.agent_session is None
    assert ctx.projects == ()


def test_context_from_claims_missing_subject_raises() -> None:
    with pytest.raises(AuthError):
        context_from_claims({"agent_session": "x"})


def test_require_project_validates_membership() -> None:
    ctx = RequestContext(principal="p", agent_session=None, projects=("a", "b"))
    assert require_project(ctx, "a") == "a"
    with pytest.raises(AuthError):
        require_project(ctx, "c")


def test_build_verifier_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_OIDC_JWKS_URI", raising=False)
    monkeypatch.setenv("KDIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("KDIVE_OIDC_AUDIENCE", AUDIENCE)
    with pytest.raises(CategorizedError, match="KDIVE_OIDC_JWKS_URI"):
        build_verifier()


def test_build_verifier_constructs_with_full_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_OIDC_JWKS_URI", "https://idp.test/jwks")
    monkeypatch.setenv("KDIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("KDIVE_OIDC_AUDIENCE", AUDIENCE)
    verifier = build_verifier()
    assert verifier.issuer == ISSUER
    assert verifier.audience == AUDIENCE


def test_context_from_claims_parses_roles() -> None:
    from kdive.security.authz.rbac import Role

    ctx = context_from_claims({"sub": "alice", "projects": ["a"], "roles": {"a": "admin"}})
    assert ctx.roles == {"a": Role.ADMIN}


def test_context_from_claims_absent_roles_is_empty() -> None:
    ctx = context_from_claims({"sub": "alice", "projects": ["a"]})
    assert ctx.roles == {}


def test_context_from_claims_parses_platform_roles() -> None:
    from kdive.security.authz.rbac import PlatformRole

    ctx = context_from_claims({"sub": "alice", "platform_roles": ["platform_auditor"]})
    assert ctx.platform_roles == frozenset({PlatformRole.PLATFORM_AUDITOR})


def test_context_from_claims_absent_platform_roles_is_empty() -> None:
    ctx = context_from_claims({"sub": "alice", "projects": ["a"]})
    assert ctx.platform_roles == frozenset()


def test_context_from_claims_rejects_non_array_platform_roles() -> None:
    with pytest.raises(AuthError):
        context_from_claims({"sub": "alice", "platform_roles": "platform_auditor"})
