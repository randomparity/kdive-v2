"""Tests for the platform-scoped RBAC tier (ADR-0043 §1/§2).

The platform tier is orthogonal to per-project roles: `platform_roles_from_claims`
parses a flat-array claim fail-closed, and `require_platform_role` enforces the
independent grants with the single `platform_admin ⊇ platform_auditor` partial order.
"""

from __future__ import annotations

import pytest

from kdive.mcp.auth import AuthError, RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    Role,
    RoleDenied,
    platform_roles_from_claims,
    require_platform_role,
    require_role,
)


def _ctx(
    *,
    projects: tuple[str, ...] = (),
    platform_roles: frozenset[PlatformRole] = frozenset(),
) -> RequestContext:
    return RequestContext(
        principal="alice",
        agent_session=None,
        projects=projects,
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


# --- Cross-axis boundary: a platform role conveys no project-scoped read access ---
#
# ADR-0043 §1/§7: the two scope axes never interact — `require_role` is "unchanged and
# unaware of platform roles." A caller on the platform axis (e.g. `operator-cli` with
# `platform_operator`, no project membership) is therefore denied a project-scoped read
# (allocations/systems/runs/ledger, all gated `require_role(ctx, project, viewer)`) exactly
# as an unauthenticated-to-that-project caller is. (Cross-project `inventory.list` is the
# *platform_auditor* read, a separate door — not a `require_role` project read.) These tests
# pin that boundary as a stated guarantee (issue #341): platform roles govern platform-wide
# ops, not tenant data.


@pytest.mark.parametrize(
    "platform_role",
    [PlatformRole.PLATFORM_OPERATOR, PlatformRole.PLATFORM_ADMIN, PlatformRole.PLATFORM_AUDITOR],
)
def test_platform_role_without_membership_denied_project_read(
    platform_role: PlatformRole,
) -> None:
    # A platform-only token (no project membership) is denied a project-scoped read at the
    # `viewer` floor. `require_role` consults only `ctx.projects`/`ctx.roles`; the held
    # platform role — even `platform_admin` — grants no project-data access.
    ctx = _ctx(projects=(), platform_roles=frozenset({platform_role}))
    with pytest.raises(AuthorizationError):
        require_role(ctx, "proj", Role.VIEWER)


def test_platform_role_project_denial_is_non_member_site_not_role_denied() -> None:
    # The denial is the non-member site (base AuthorizationError), not the member-over-reach
    # site (RoleDenied): a platform-only token is not a project member, so the dispatch
    # boundary does not audit it as a per-project rank-below denial.
    ctx = _ctx(projects=(), platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}))
    with pytest.raises(AuthorizationError) as excinfo:
        require_role(ctx, "proj", Role.VIEWER)
    assert not isinstance(excinfo.value, RoleDenied)


def test_platform_admin_membership_on_other_project_denied_target_project_read() -> None:
    # Even combining a platform role with membership on a *different* project does not reach
    # across to the target project: the axes compose independently, no implicit cross-tenant
    # read. Holds `viewer` on "other" + `platform_admin`; still denied a read on "proj".
    ctx = RequestContext(
        principal="alice",
        agent_session=None,
        projects=("other",),
        roles={"other": Role.VIEWER},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
    )
    with pytest.raises(AuthorizationError):
        require_role(ctx, "proj", Role.VIEWER)
