"""Tests for project-scoped RBAC (ADR-0006, ADR-0020)."""

from __future__ import annotations

import pytest

from kdive.mcp.auth import AuthError, RequestContext
from kdive.security.rbac import AuthorizationError, Role, require_role, roles_from_claims


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


def test_require_role_too_low_denied() -> None:
    with pytest.raises(AuthorizationError):
        require_role(_ctx(roles={"proj": Role.OPERATOR}), "proj", Role.ADMIN)


def test_require_role_not_a_member_denied() -> None:
    with pytest.raises(AuthorizationError):
        require_role(_ctx(projects=("other",), roles={"proj": Role.ADMIN}), "proj", Role.VIEWER)


def test_require_role_member_without_role_denied_not_keyerror() -> None:
    # The common token shape: granted membership, no per-project role.
    with pytest.raises(AuthorizationError):
        require_role(_ctx(projects=("proj",), roles={}), "proj", Role.VIEWER)


def test_request_context_with_roles_is_hashable() -> None:
    ctx = _ctx(roles={"proj": Role.ADMIN})
    assert hash(ctx) == hash(ctx)  # does not raise despite the dict field
    assert ctx.roles["proj"] is Role.ADMIN
