"""RequestContext derivation from verified token claims."""

from __future__ import annotations

import pytest

from kdive.security.authz.context import (
    RequestContext,
    context_from_claims,
    require_project,
)
from kdive.security.authz.errors import AuthError, ProjectMembershipDenied


def test_context_from_claims_rejects_non_string_projects() -> None:
    with pytest.raises(AuthError, match="projects claim entries must be non-empty strings"):
        context_from_claims({"sub": "alice", "projects": ["proj", 7]})


def test_context_from_claims_rejects_empty_project_names() -> None:
    with pytest.raises(AuthError, match="projects claim entries must be non-empty strings"):
        context_from_claims({"sub": "alice", "projects": ["proj", ""]})


def test_project_membership_denied_is_an_auth_error() -> None:
    # The dispatch boundary catches the subclass; existing AuthError catches still apply
    # because ProjectMembershipDenied IS an AuthError (ADR-0098).
    assert issubclass(ProjectMembershipDenied, AuthError)


def test_require_project_grants_member_project() -> None:
    ctx = RequestContext(principal="p", agent_session=None, projects=("a", "b"))
    assert require_project(ctx, "a") == "a"


def test_require_project_raises_membership_denied_for_non_member() -> None:
    ctx = RequestContext(principal="p", agent_session=None, projects=("a",))
    with pytest.raises(ProjectMembershipDenied, match="not granted"):
        require_project(ctx, "c")
