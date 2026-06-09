"""Property-based adversarial tests for claim parsing and RBAC (no DB).

Targets the fail-closed promises of the auth boundary:
  * `roles_from_claims` never invents, upgrades, or silently drops a grant — a
    malformed claim raises rather than yielding a usable role;
  * `context_from_claims` rejects an unusable subject and malformed claim shapes;
  * `require_role` is monotone in the role rank and denies non-members / no-role.
A hypothesis counterexample here is a privilege-boundary defect.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kdive.mcp.auth import AuthError, RequestContext, context_from_claims, require_project
from kdive.security.authz.rbac import AuthorizationError, Role, require_role, roles_from_claims

_ROLE_NAMES = [r.value for r in Role]
_RANK = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}

# Strings that are never a valid role name.
_bad_role_text = st.text().filter(lambda s: s not in _ROLE_NAMES)
_project_names = st.text(min_size=1, max_size=12)


@given(roles=st.dictionaries(_project_names, st.sampled_from(_ROLE_NAMES), max_size=6))
def test_roles_from_claims_roundtrips_valid_grants(roles: dict[str, str]) -> None:
    parsed = roles_from_claims({"roles": roles})
    assert parsed == {p: Role(v) for p, v in roles.items()}
    # No grant is ever upgraded above what the claim stated.
    for project, value in roles.items():
        assert _RANK[parsed[project]] == _RANK[Role(value)]


@given(
    good=st.dictionaries(_project_names, st.sampled_from(_ROLE_NAMES), max_size=3),
    bad_project=_project_names,
    bad_value=_bad_role_text,
)
def test_roles_from_claims_one_bad_value_fails_closed(
    good: dict[str, str], bad_project: str, bad_value: str
) -> None:
    claim = {**good, bad_project: bad_value}
    with pytest.raises(AuthError):
        roles_from_claims({"roles": claim})


@given(raw=st.one_of(st.text(), st.integers(), st.lists(st.text()), st.booleans()))
def test_roles_from_claims_non_object_fails_closed(raw: object) -> None:
    with pytest.raises(AuthError):
        roles_from_claims({"roles": raw})


@given(st.data())
def test_roles_from_claims_non_string_value_fails_closed(data: st.DataObject) -> None:
    project = data.draw(_project_names)
    non_string = data.draw(st.one_of(st.integers(), st.none(), st.lists(st.text())))
    with pytest.raises(AuthError):
        roles_from_claims({"roles": {project: non_string}})


@given(
    bad_project=st.one_of(st.integers(), st.none(), st.booleans(), st.just("")),
    value=st.sampled_from(_ROLE_NAMES),
)
def test_roles_from_claims_bad_project_key_fails_closed(bad_project: object, value: str) -> None:
    with pytest.raises(AuthError):
        roles_from_claims({"roles": cast(Any, {bad_project: value})})


def test_roles_from_claims_absent_is_empty() -> None:
    assert roles_from_claims({}) == {}
    assert roles_from_claims({"roles": None}) == {}


@given(
    held=st.sampled_from(list(Role)),
    required=st.sampled_from(list(Role)),
)
def test_require_role_is_monotone_in_rank(held: Role, required: Role) -> None:
    ctx = RequestContext(
        principal="p", agent_session=None, projects=("proj",), roles={"proj": held}
    )
    if _RANK[held] >= _RANK[required]:
        require_role(ctx, "proj", required)  # must not raise
    else:
        with pytest.raises(AuthorizationError):
            require_role(ctx, "proj", required)


@given(required=st.sampled_from(list(Role)))
def test_require_role_member_without_role_is_denied(required: Role) -> None:
    # In `projects` but absent from the roles map: membership is not a role.
    ctx = RequestContext(principal="p", agent_session=None, projects=("proj",), roles={})
    with pytest.raises(AuthorizationError):
        require_role(ctx, "proj", required)


@given(required=st.sampled_from(list(Role)))
def test_require_role_non_member_is_denied_even_with_role_claim(required: Role) -> None:
    # A role granted for a project NOT in `projects` must never authorize: the
    # membership check (projects) and the role check must both pass.
    ctx = RequestContext(principal="p", agent_session=None, projects=(), roles={"proj": Role.ADMIN})
    with pytest.raises(AuthorizationError):
        require_role(ctx, "proj", required)


@given(subject=st.one_of(st.none(), st.just(""), st.integers(), st.booleans()))
def test_context_from_claims_rejects_unusable_subject(subject: object) -> None:
    with pytest.raises(AuthError):
        context_from_claims({"sub": subject})


@given(
    projects=st.one_of(
        st.text(min_size=1),
        st.integers(),  # includes 0 — the falsy value the pre-fix `or ()` swallowed
        st.dictionaries(st.text(), st.text()),  # includes {} — also previously swallowed
        st.booleans(),
    )
)
def test_context_from_claims_rejects_non_list_projects(projects: object) -> None:
    # Every non-list `projects` claim — falsy (0, "", False, {}) or truthy — is malformed
    # and must raise, never silently coerce to "no projects granted".
    with pytest.raises(AuthError):
        context_from_claims({"sub": "alice", "projects": projects})


@given(
    sub=st.text(min_size=1, max_size=10),
    projects=st.lists(_project_names, max_size=5),
)
def test_require_project_matches_context_membership(sub: str, projects: list[str]) -> None:
    ctx = context_from_claims({"sub": sub, "projects": projects})
    assert ctx.principal == sub
    for project in projects:
        assert require_project(ctx, project) == project
    if "absent-project" not in projects:
        with pytest.raises(AuthError):
            require_project(ctx, "absent-project")
