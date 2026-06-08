"""Tests for project-scoped RBAC (ADR-0006, ADR-0020)."""

from __future__ import annotations

import pytest

from kdive.mcp.auth import AuthError, RequestContext
from kdive.security.rbac import (
    AuthorizationError,
    Role,
    RoleDenied,
    require_role,
    roles_from_claims,
)


def _ctx(
    *, projects: tuple[str, ...] = ("proj",), roles: dict[str, Role] | None = None
) -> RequestContext:
    return RequestContext(
        principal="alice", agent_session=None, projects=projects, roles=roles or {}
    )


def test_roles_from_claims_absent_is_empty() -> None:
    assert roles_from_claims({"sub": "alice"}) == {}


def test_roles_from_claims_parses_map() -> None:
    assert roles_from_claims({"roles": {"a": "admin", "b": "operator"}}) == {
        "a": Role.ADMIN,
        "b": Role.OPERATOR,
    }


def test_roles_from_claims_rejects_non_object() -> None:
    with pytest.raises(AuthError):
        roles_from_claims({"roles": ["admin"]})


def test_roles_from_claims_rejects_unknown_role() -> None:
    with pytest.raises(AuthError):
        roles_from_claims({"roles": {"a": "superadmin"}})


def test_roles_from_claims_rejects_non_string_value() -> None:
    with pytest.raises(AuthError):
        roles_from_claims({"roles": {"a": 1}})


def test_require_role_admin_satisfies_operator() -> None:
    require_role(_ctx(roles={"proj": Role.ADMIN}), "proj", Role.OPERATOR)


def test_require_role_exact_match_ok() -> None:
    require_role(_ctx(roles={"proj": Role.OPERATOR}), "proj", Role.OPERATOR)


def test_require_role_too_low_raises_role_denied() -> None:
    # The member-over-reach (rank-below) site raises the dedicated RoleDenied subclass,
    # the discriminator the dispatch boundary catches to audit the denial.
    with pytest.raises(RoleDenied):
        require_role(_ctx(roles={"proj": Role.OPERATOR}), "proj", Role.ADMIN)


def test_role_denied_carries_project_principal_and_roles() -> None:
    # The exception is the only carrier of project at the dispatch boundary (object-
    # resolving tools resolve project from the row, not the call args).
    ctx = _ctx(projects=("proj",), roles={"proj": Role.OPERATOR})
    with pytest.raises(RoleDenied) as excinfo:
        require_role(ctx, "proj", Role.ADMIN)
    denial = excinfo.value
    assert denial.project == "proj"
    assert denial.principal == "alice"
    assert denial.held is Role.OPERATOR
    assert denial.required is Role.ADMIN


def test_require_role_member_without_role_raises_role_denied() -> None:
    # The common token shape: granted membership, no per-project role. Membership is
    # held, so this is the rank-below (member-over-reach) site → RoleDenied, held=None.
    ctx = _ctx(projects=("proj",), roles={})
    with pytest.raises(RoleDenied) as excinfo:
        require_role(ctx, "proj", Role.VIEWER)
    assert excinfo.value.held is None
    assert excinfo.value.project == "proj"


def test_require_role_not_a_member_raises_base_authorization_error_not_role_denied() -> None:
    # The non-member site keeps the base AuthorizationError so the dispatch boundary
    # (which catches RoleDenied specifically) never audits it — no write-amplification.
    with pytest.raises(AuthorizationError) as excinfo:
        require_role(_ctx(projects=("other",), roles={"proj": Role.ADMIN}), "proj", Role.VIEWER)
    assert not isinstance(excinfo.value, RoleDenied)


def test_role_denied_is_authorization_error_subclass() -> None:
    # Subclass relationship is load-bearing: the gate's `except AuthorizationError`
    # still catches a rank-below denial from require_role.
    assert issubclass(RoleDenied, AuthorizationError)


def test_request_context_with_roles_is_hashable() -> None:
    ctx = _ctx(roles={"proj": Role.ADMIN})
    assert hash(ctx) == hash(ctx)  # does not raise despite the dict field
    assert ctx.roles["proj"] is Role.ADMIN
