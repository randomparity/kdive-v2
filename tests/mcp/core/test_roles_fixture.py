"""Tests for the separated-role mock-OIDC fixture (tests/mcp/roles.py).

The fixture is the reusable artifact issue #68 ships for #71+ to import, so it carries
its own contract test: distinct principals per role, two projects, tokens that verify
against the shared keypair and derive to the role the fixture claims.
"""

from __future__ import annotations

import asyncio

from fastmcp.server.auth.providers.jwt import JWTVerifier

from kdive.mcp.auth import context_from_claims
from kdive.security.authz.rbac import Role
from tests.mcp.conftest import AUDIENCE, ISSUER
from tests.mcp.roles import PROJECT_A, PROJECT_B, PROJECTS, make_role_fixture


def test_fixture_has_three_roles_across_two_projects() -> None:
    fx = make_role_fixture()
    assert PROJECTS == (PROJECT_A, PROJECT_B)
    for project in PROJECTS:
        pp = fx.project(project)
        assert pp.viewer.role is Role.VIEWER
        assert pp.operator.role is Role.OPERATOR
        assert pp.admin.role is Role.ADMIN
        # Each principal is scoped to exactly its one project, holding exactly its role.
        for principal in (pp.viewer, pp.operator, pp.admin):
            assert principal.ctx.projects == (project,)
            assert principal.ctx.roles == {project: principal.role}


def test_fixture_principals_are_distinct_subjects() -> None:
    fx = make_role_fixture()
    subjects = {p.subject for pp in (fx.a, fx.b) for p in (pp.viewer, pp.operator, pp.admin)}
    assert len(subjects) == 6  # 3 roles x 2 projects, all distinct


def test_fixture_tokens_verify_and_derive_to_claimed_role() -> None:
    fx = make_role_fixture()
    verifier = JWTVerifier(public_key=fx.keypair.public_key, issuer=ISSUER, audience=AUDIENCE)

    async def _run() -> None:
        admin = fx.a.admin
        verified = await verifier.verify_token(admin.token)
        assert verified is not None
        derived = context_from_claims(verified.claims)
        assert derived == admin.ctx
        assert derived.roles == {PROJECT_A: Role.ADMIN}

    asyncio.run(_run())


def test_fixture_project_b_principal_not_member_of_a() -> None:
    fx = make_role_fixture()
    b_admin = fx.b.admin
    assert PROJECT_A not in b_admin.ctx.projects  # cross-project access is testable


def test_project_principals_of_returns_matching_role() -> None:
    fx = make_role_fixture()
    pp = fx.project(PROJECT_A)
    assert pp.of(Role.OPERATOR) is pp.operator
    assert pp.of(Role.ADMIN) is pp.admin
    assert pp.of(Role.VIEWER) is pp.viewer
