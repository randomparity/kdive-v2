"""Project-scoped RBAC: roles, claim parsing, and enforcement (ADR-0006, ADR-0020).

The three M0 roles form a total rank, so a higher role satisfies a lower requirement.
`roles_from_claims` turns a verified token's `roles` claim into the per-project role
map carried on `RequestContext`; `require_role` is the enforcement point every plane
tool calls before a privileged operation. A denial raises `AuthorizationError`
(distinct from `kdive.mcp.auth.AuthError`, which covers authentication/membership), so
a handler maps "you may not do this" separately from "who are you".
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kdive.mcp.auth import RequestContext

_ROLES_CLAIM = "roles"


class Role(StrEnum):
    """The three project-scoped M0 roles, ordered viewer < operator < admin."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}


class AuthorizationError(Exception):
    """A verified, authenticated principal may not perform the requested operation.

    Distinct from `kdive.mcp.auth.AuthError` (no subject / project not granted): the
    caller is known and a project member, but lacks the role the operation needs.
    """


def roles_from_claims(claims: Mapping[str, object]) -> dict[str, Role]:
    """Parse the per-project role map from a verified token's ``roles`` claim.

    The claim is a JSON object mapping a project name to one role string
    (``{"proj-a": "admin"}``). An absent claim yields ``{}`` (membership without a
    role).

    Raises:
        AuthError: The claim is present but not an object, or a value is not a string
            or not a known role (fail closed — never silently drop or upgrade a grant).
    """
    raw = claims.get(_ROLES_CLAIM)
    if raw is None:
        return {}
    # Function-level import: the only runtime rbac->auth edge, kept here so rbac's
    # module-level dependency on auth stays type-only and the import cycle is broken.
    from kdive.mcp.auth import AuthError

    if not isinstance(raw, Mapping):
        raise AuthError("roles claim is not an object")
    roles: dict[str, Role] = {}
    for project, value in raw.items():
        if not isinstance(value, str):
            raise AuthError(f"roles claim value for project {project!r} is not a string")
        try:
            role = Role(value)
        except ValueError:
            raise AuthError(
                f"roles claim has unknown role {value!r} for project {project!r}"
            ) from None
        roles[str(project)] = role
    return roles


def require_role(ctx: RequestContext, project: str, role: Role) -> None:
    """Enforce that ``ctx`` holds at least ``role`` on ``project``.

    Raises:
        AuthorizationError: ``project`` is not granted to the principal, the principal
            carries no role on it, or the held role ranks below ``role``.
    """
    if project not in ctx.projects:
        raise AuthorizationError(f"{ctx.principal!r} is not a member of project {project!r}")
    held = ctx.roles.get(project)
    if held is None or _RANK[held] < _RANK[role]:
        held_name = held.value if held is not None else "none"
        raise AuthorizationError(
            f"{ctx.principal!r} needs role {role.value!r} on project {project!r}; "
            f"holds {held_name!r}"
        )
