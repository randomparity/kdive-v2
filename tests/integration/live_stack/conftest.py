"""Preflight helpers for the wire-harness smoke tiers (the ADR-0035 §4 skip idiom)."""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

from tests.integration.live_stack.harness import OidcIssuer


def _issuer_reachable(issuer: OidcIssuer) -> bool:
    try:
        with urllib.request.urlopen(issuer.jwks_uri, timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def require_issuer() -> OidcIssuer:
    """Skip unless the mock-OIDC issuer is configured and its JWKS is reachable."""
    base_url = os.environ.get("KDIVE_OIDC_ISSUER")
    if not base_url:
        pytest.skip("KDIVE_OIDC_ISSUER unset; start the issuer (`docker compose up -d oidc`)")
    issuer = OidcIssuer.from_env()
    if not _issuer_reachable(issuer):
        pytest.skip(f"mock-OIDC issuer JWKS unreachable at {issuer.jwks_uri}")
    return issuer


def require_stack() -> str:
    """Skip unless a kdive server base URL is configured (the live_stack tier)."""
    base_url = os.environ.get("KDIVE_STACK_BASE_URL")
    if not base_url:
        pytest.skip("KDIVE_STACK_BASE_URL unset; bring up the stack (see the live-stack runbook)")
    return base_url
