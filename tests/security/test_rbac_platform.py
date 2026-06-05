"""Tests for the platform-scoped RBAC tier (ADR-0043 §1/§2).

The platform tier is orthogonal to per-project roles: `platform_roles_from_claims`
parses a flat-array claim fail-closed, and `require_platform_role` enforces the
independent grants with the single `platform_admin ⊇ platform_auditor` partial order.
"""

from __future__ import annotations

import pytest

from kdive.mcp.auth import AuthError, RequestContext
from kdive.security.rbac import (
    AuthorizationError,
    PlatformRole,
    platform_roles_from_claims,
    require_platform_role,
)


def _ctx(*, platform_roles: frozenset[PlatformRole] = frozenset()) -> RequestContext:
    return RequestContext(
        principal="alice",
        agent_session=None,
        projects=(),
        platform_roles=platform_roles,
    )


def test_platform_roles_from_claims_absent_is_empty() -> None:
    assert platform_roles_from_claims({"sub": "alice"}) == frozenset()


def test_platform_roles_from_claims_parses_array() -> None:
    parsed = platform_roles_from_claims(
        {"platform_roles": ["platform_auditor", "platform_operator"]}
    )
    assert parsed == frozenset({PlatformRole.PLATFORM_AUDITOR, PlatformRole.PLATFORM_OPERATOR})


def test_platform_roles_from_claims_parses_single_entry() -> None:
    assert platform_roles_from_claims({"platform_roles": ["platform_admin"]}) == frozenset(
        {PlatformRole.PLATFORM_ADMIN}
    )


def test_platform_roles_from_claims_rejects_non_array() -> None:
    # A map (the per-project `roles` shape) is not a valid flat array — fail closed.
    with pytest.raises(AuthError):
        platform_roles_from_claims({"platform_roles": {"platform_auditor": True}})


def test_platform_roles_from_claims_rejects_string() -> None:
    # A bare string is iterable but not the array contract; reject rather than parse
    # it character by character.
    with pytest.raises(AuthError):
        platform_roles_from_claims({"platform_roles": "platform_auditor"})


def test_platform_roles_from_claims_rejects_unknown_role() -> None:
    with pytest.raises(AuthError):
        platform_roles_from_claims({"platform_roles": ["platform_superuser"]})


def test_platform_roles_from_claims_rejects_non_string_entry() -> None:
    with pytest.raises(AuthError):
        platform_roles_from_claims({"platform_roles": ["platform_auditor", 1]})


def test_platform_roles_from_claims_does_not_confuse_project_role() -> None:
    # A per-project role string is not a platform role; the namespaces are disjoint.
    with pytest.raises(AuthError):
        platform_roles_from_claims({"platform_roles": ["admin"]})


@pytest.mark.parametrize(
    "role",
    [PlatformRole.PLATFORM_ADMIN, PlatformRole.PLATFORM_OPERATOR, PlatformRole.PLATFORM_AUDITOR],
)
def test_require_platform_role_each_role_satisfies_itself(role: PlatformRole) -> None:
    require_platform_role(_ctx(platform_roles=frozenset({role})), role)


def test_require_platform_role_admin_satisfies_auditor() -> None:
    require_platform_role(
        _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN})),
        PlatformRole.PLATFORM_AUDITOR,
    )


def test_require_platform_role_admin_does_not_satisfy_operator() -> None:
    with pytest.raises(AuthorizationError):
        require_platform_role(
            _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN})),
            PlatformRole.PLATFORM_OPERATOR,
        )


def test_require_platform_role_operator_satisfies_neither_other() -> None:
    for needed in (PlatformRole.PLATFORM_AUDITOR, PlatformRole.PLATFORM_ADMIN):
        with pytest.raises(AuthorizationError):
            require_platform_role(
                _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR})), needed
            )


def test_require_platform_role_auditor_does_not_satisfy_admin() -> None:
    with pytest.raises(AuthorizationError):
        require_platform_role(
            _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR})),
            PlatformRole.PLATFORM_ADMIN,
        )


def test_require_platform_role_empty_set_denied() -> None:
    with pytest.raises(AuthorizationError):
        require_platform_role(_ctx(), PlatformRole.PLATFORM_AUDITOR)
