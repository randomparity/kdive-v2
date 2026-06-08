"""Separated-role mock-OIDC principals for M1 RBAC tests (ADR-0037 decision 3).

M0's convenience of one principal holding both roles ends in M1: the role boundary is
only *verified* if a `viewer`/`operator`/`admin` are distinct principals, so a negative
test can prove the lower role is refused. This module mints those separated principals
across **two** projects, so cross-project access (a principal of project A reaching a
project-B object) can be tested too.

Each principal is exposed two ways so both test styles can reuse it without re-deriving
roles:

* ``.token`` — a signed JWT carrying the kdive ``projects`` + ``roles`` claims, for
  app/transport-level tests that drive the real ``JWTVerifier``.
* ``.ctx`` — the :class:`~kdive.mcp.auth.RequestContext` the token derives to, for the
  handler-unit tests that call a tool's plain async handler directly with an injected
  ``ctx`` (the repo's primary test contract).

The single ``RSAKeyPair`` is shared so every token in one :class:`RoleFixture` verifies
against one public key. Importable by other test modules (issue #71 reuses it); building
the fixture mints tokens but opens no network or DB connection.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastmcp.server.auth.providers.jwt import RSAKeyPair

from kdive.mcp.auth import RequestContext, context_from_claims
from kdive.security.authz.rbac import Role
from tests.mcp.conftest import make_keypair, mint

PROJECT_A = "proj-a"
PROJECT_B = "proj-b"
PROJECTS = (PROJECT_A, PROJECT_B)


@dataclass(frozen=True)
class Principal:
    """One separated-role principal: its signed token and the context it derives to."""

    subject: str
    project: str
    role: Role
    token: str
    ctx: RequestContext


@dataclass(frozen=True)
class ProjectPrincipals:
    """The three separated principals (viewer/operator/admin) for one project."""

    project: str
    viewer: Principal
    operator: Principal
    admin: Principal

    def of(self, role: Role) -> Principal:
        """Return this project's principal holding exactly ``role``."""
        return {Role.VIEWER: self.viewer, Role.OPERATOR: self.operator, Role.ADMIN: self.admin}[
            role
        ]


@dataclass(frozen=True)
class RoleFixture:
    """Separated-role principals across two projects, all signed by one keypair."""

    keypair: RSAKeyPair
    a: ProjectPrincipals
    b: ProjectPrincipals

    def project(self, project: str) -> ProjectPrincipals:
        """Return the per-role principals for ``project`` (``PROJECT_A``/``PROJECT_B``)."""
        if project == PROJECT_A:
            return self.a
        if project == PROJECT_B:
            return self.b
        raise KeyError(f"no separated-role principals for project {project!r}")


def _principal(keypair: RSAKeyPair, project: str, role: Role) -> Principal:
    subject = f"{role.value}-{project}"
    token = mint(
        keypair,
        subject=subject,
        agent_session=f"sess-{subject}",
        projects=[project],
        roles={project: role.value},
    )
    claims: dict[str, object] = {
        "sub": subject,
        "agent_session": f"sess-{subject}",
        "projects": [project],
        "roles": {project: role.value},
    }
    return Principal(
        subject=subject, project=project, role=role, token=token, ctx=context_from_claims(claims)
    )


def _project_principals(keypair: RSAKeyPair, project: str) -> ProjectPrincipals:
    return ProjectPrincipals(
        project=project,
        viewer=_principal(keypair, project, Role.VIEWER),
        operator=_principal(keypair, project, Role.OPERATOR),
        admin=_principal(keypair, project, Role.ADMIN),
    )


def make_role_fixture() -> RoleFixture:
    """Mint the separated-role principals for both projects under one shared keypair.

    Returns:
        A :class:`RoleFixture` exposing ``viewer``/``operator``/``admin`` principals for
        ``PROJECT_A`` and ``PROJECT_B``, each carrying a signed token and the derived
        ``RequestContext``.
    """
    keypair = make_keypair()
    return RoleFixture(
        keypair=keypair,
        a=_project_principals(keypair, PROJECT_A),
        b=_project_principals(keypair, PROJECT_B),
    )
