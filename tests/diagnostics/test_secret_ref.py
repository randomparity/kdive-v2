"""`secret_ref` check tests — full coverage, non-disclosure reporting (ADR-0091 §2).

Server vantage. Every configured secret ref must resolve in the backend. The motivating
M2 fault was a ref that did not resolve, so coverage spans **both** platform and project
refs — but non-disclosure is enforced on the *reporting* surface: the verdict reports
aggregate pass/fail counts and platform-ref detail only, **never** per-tenant/project ref
identifiers. A backend that cannot be reached at all is `error`, not a contract `fail`.
"""

from __future__ import annotations

import asyncio

from kdive.diagnostics.checks import CheckStatus, SecretRefCheck

_PLATFORM_REF = "platform/oidc-secret"
_PROJECT_REF = "project/acme/db-password"


def _refs() -> list[tuple[str, bool]]:
    """A platform ref and a per-tenant ref, both presented to the resolver."""
    return [(_PLATFORM_REF, True), (_PROJECT_REF, False)]


def test_all_refs_resolve_is_pass() -> None:
    check = SecretRefCheck(refs=_refs(), resolve=lambda ref: None)
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS
    assert result.provider is None
    assert "2" in result.detail  # aggregate count of resolved refs


def test_unresolved_ref_is_fail_with_fix() -> None:
    def _resolve(ref: str) -> None:
        if ref == _PROJECT_REF:
            raise FileNotFoundError(ref)

    check = SecretRefCheck(refs=_refs(), resolve=_resolve)
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL
    assert result.fix == (
        "secret ref does not resolve under KDIVE_SECRETS_ROOT; create the file-ref or fix the path"
    )


def test_unresolved_project_ref_is_never_disclosed() -> None:
    def _resolve(ref: str) -> None:
        if ref == _PROJECT_REF:
            raise FileNotFoundError(ref)

    check = SecretRefCheck(refs=_refs(), resolve=_resolve)
    result = asyncio.run(check.run())
    # Aggregate counts surface; the per-tenant ref identifier never does.
    assert _PROJECT_REF not in result.detail
    assert "acme" not in result.detail
    assert result.fix is not None and _PROJECT_REF not in result.fix
    assert "1" in result.detail  # one unresolved


def test_unresolved_platform_ref_may_be_named() -> None:
    def _resolve(ref: str) -> None:
        if ref == _PLATFORM_REF:
            raise FileNotFoundError(ref)

    check = SecretRefCheck(refs=_refs(), resolve=_resolve)
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL
    # A platform ref is operator-owned config, not tenant data — it may be named.
    assert _PLATFORM_REF in result.detail


def test_backend_unreachable_is_error_not_fail() -> None:
    def _resolve(ref: str) -> None:
        raise ConnectionError("secret backend unreachable")

    check = SecretRefCheck(
        refs=_refs(),
        resolve=_resolve,
        backend_unreachable=ConnectionError,
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.ERROR
    assert result.fix is None
    assert _PROJECT_REF not in result.detail


def test_no_configured_refs_is_pass() -> None:
    check = SecretRefCheck(refs=[], resolve=lambda ref: None)
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS
