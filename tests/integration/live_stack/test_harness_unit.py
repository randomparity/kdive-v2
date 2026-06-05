"""Unit tests for the network-free parts of the wire harness (claims + issuer config)."""

from __future__ import annotations

import pytest

from tests.integration.live_stack.harness import OidcIssuer, _build_claims


def test_build_claims_nested_roles_object() -> None:
    claims = _build_claims(
        subject="admin-proj-a",
        audience="kdive",
        projects=["proj-a"],
        roles={"proj-a": "admin"},
        platform_roles=None,
        agent_session="sess-1",
    )
    assert claims["sub"] == "admin-proj-a"
    assert claims["aud"] == "kdive"
    assert claims["projects"] == ["proj-a"]
    assert claims["roles"] == {"proj-a": "admin"}  # nested object, not flat
    assert claims["agent_session"] == "sess-1"
    assert "platform_roles" not in claims  # None -> omitted


def test_build_claims_platform_roles_array() -> None:
    claims = _build_claims(
        subject="auditor",
        audience="kdive",
        projects=["proj-a"],
        roles={"proj-a": "viewer"},
        platform_roles=["platform_auditor"],
        agent_session=None,
    )
    assert claims["platform_roles"] == ["platform_auditor"]  # flat array
    assert "agent_session" not in claims  # None -> omitted


def test_build_claims_empty_platform_roles_is_present_but_empty() -> None:
    claims = _build_claims(
        subject="x",
        audience="kdive",
        projects=[],
        roles={},
        platform_roles=[],
        agent_session=None,
    )
    assert claims["platform_roles"] == []  # [] -> present-but-empty, distinct from None
    assert claims["projects"] == []
    assert claims["roles"] == {}


def test_oidc_issuer_derived_endpoints() -> None:
    issuer = OidcIssuer(
        base_url="http://localhost:8090/default", audience="kdive", client_id="kdive-test"
    )
    assert issuer.authorize_endpoint == "http://localhost:8090/default/authorize"
    assert issuer.token_endpoint == "http://localhost:8090/default/token"
    assert issuer.jwks_uri == "http://localhost:8090/default/jwks"


def test_oidc_issuer_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_OIDC_ISSUER", "http://localhost:8090/default")
    monkeypatch.setenv("KDIVE_OIDC_AUDIENCE", "kdive")
    monkeypatch.delenv("KDIVE_OIDC_CLIENT_ID", raising=False)
    issuer = OidcIssuer.from_env()
    assert issuer.base_url == "http://localhost:8090/default"
    assert issuer.audience == "kdive"
    assert issuer.client_id == "kdive-test"  # default


def test_oidc_issuer_from_env_missing_base_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_OIDC_ISSUER", raising=False)
    with pytest.raises(RuntimeError, match="KDIVE_OIDC_ISSUER"):
        OidcIssuer.from_env()
