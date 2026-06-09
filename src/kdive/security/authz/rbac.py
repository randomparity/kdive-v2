"""Project-scoped RBAC: roles, claim parsing, and enforcement (ADR-0006, ADR-0020).

The three project roles form a total rank, so a higher role satisfies a lower requirement.
`roles_from_claims` turns a verified token's `roles` claim into the per-project role
map carried on `RequestContext`; `require_role` is the enforcement point every plane
tool calls before a privileged operation. A denial raises `AuthorizationError`
(distinct from `kdive.security.authz.errors.AuthError`, which covers authentication/membership), so
a handler maps "you may not do this" separately from "who are you".
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING

from kdive.security.authz.errors import AuthError

if TYPE_CHECKING:
    from kdive.security.authz.context import RequestContext

_ROLES_CLAIM = "roles"
_PLATFORM_ROLES_CLAIM = "platform_roles"


class Role(StrEnum):
    """The three project-scoped roles, ordered viewer < operator < admin."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}


class PlatformRole(StrEnum):
    """The three platform-scoped roles (ADR-0043 §2).

    Granted **independently** — not a ``viewer < operator < admin`` rank — to preserve
    separation of duties (an infra operator does not thereby read every project's data;
    an auditor cannot mutate). The one deliberate partial-order exception is encoded in
    :data:`_PLATFORM_IMPLIES`.
    """

    PLATFORM_ADMIN = "platform_admin"
    PLATFORM_OPERATOR = "platform_operator"
    PLATFORM_AUDITOR = "platform_auditor"


# The single partial-order exception: `platform_admin` satisfies `platform_auditor`
# (break-glass mutation requires visibility of what it mutates); `platform_operator`
# satisfies neither (ADR-0043 §2). Every role satisfies itself (handled in
# `require_platform_role` directly, so this maps only the cross-role implications).
_PLATFORM_IMPLIES: dict[PlatformRole, frozenset[PlatformRole]] = {
    PlatformRole.PLATFORM_ADMIN: frozenset({PlatformRole.PLATFORM_AUDITOR}),
}


class AuthorizationError(Exception):
    """A verified, authenticated principal may not perform the requested operation.

    Distinct from `kdive.security.authz.errors.AuthError` (no subject / project not granted): the
    caller is known and a project member, but lacks the role the operation needs.
    """


class RoleDenied(AuthorizationError):
    """A project **member** holds a role ranking below the required one (ADR-0062 §5).

    Raised at :func:`require_role`'s **rank-below site only** — never at the non-member
    site, which keeps the base :class:`AuthorizationError`. The dedicated subclass is the
    discriminator the MCP dispatch boundary catches to audit a member-over-reach denial
    (and *only* that case): a base-class catch would also sweep in `require_platform_role`
    denials and :class:`~kdive.security.authz.gate.DestructiveOpDenied` (both
    ``AuthorizationError`` subclasses), double-writing them.

    Carries ``project`` because the dispatch boundary cannot recover it from call args for
    object-resolving tools (which resolve ``project`` from the row at runtime); the
    exception is the only carrier, so the audited denial row's ``project`` comes from here.
    """

    def __init__(self, *, principal: str, project: str, held: Role | None, required: Role) -> None:
        self.principal = principal
        self.project = project
        self.held = held
        self.required = required
        held_name = held.value if held is not None else "none"
        super().__init__(
            f"{principal!r} needs role {required.value!r} on project {project!r}; "
            f"holds {held_name!r}"
        )


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

    if not isinstance(raw, Mapping):
        raise AuthError("roles claim is not an object")
    roles: dict[str, Role] = {}
    for project, value in raw.items():
        if not isinstance(project, str) or not project:
            raise AuthError(f"roles claim project key {project!r} is not a non-empty string")
        if not isinstance(value, str):
            raise AuthError(f"roles claim value for project {project!r} is not a string")
        try:
            role = Role(value)
        except ValueError:
            raise AuthError(
                f"roles claim has unknown role {value!r} for project {project!r}"
            ) from None
        roles[project] = role
    return roles


def require_role(ctx: RequestContext, project: str, role: Role) -> None:
    """Enforce that ``ctx`` holds at least ``role`` on ``project``.

    Raises:
        AuthorizationError: ``project`` is not granted to the principal (the non-member
            site — the base class, never audited at the dispatch boundary).
        RoleDenied: The principal is a member whose held role (possibly none) ranks below
            ``role`` (the member-over-reach site — audited at the dispatch boundary).
    """
    if project not in ctx.projects:
        raise AuthorizationError(f"{ctx.principal!r} is not a member of project {project!r}")
    held = ctx.roles.get(project)
    if held is None or _RANK[held] < _RANK[role]:
        raise RoleDenied(principal=ctx.principal, project=project, held=held, required=role)


def platform_roles_from_claims(claims: Mapping[str, object]) -> frozenset[PlatformRole]:
    """Parse the platform-role set from a verified token's ``platform_roles`` claim.

    The claim is a **flat array** of role strings (``["platform_auditor"]``), separate
    from the per-project ``roles`` map. An absent claim yields the empty set (an ordinary
    project token holds no platform role).

    Raises:
        AuthError: The claim is present but not an array (a string or object is rejected,
            not iterated), or an entry is not a string or not a known platform role
            (fail closed — mirrors :func:`roles_from_claims`, never silently drops a
            value).
    """
    raw = claims.get(_PLATFORM_ROLES_CLAIM)
    if raw is None:
        return frozenset()

    # A str is a Sequence too; exclude it so a bare role string is not iterated as
    # characters (fail closed on the wrong claim shape).
    if not isinstance(raw, (list, tuple)) or isinstance(raw, str):
        raise AuthError("platform_roles claim is not an array")
    roles: set[PlatformRole] = set()
    for value in raw:
        if not isinstance(value, str):
            raise AuthError(f"platform_roles claim entry {value!r} is not a string")
        try:
            roles.add(PlatformRole(value))
        except ValueError:
            raise AuthError(f"platform_roles claim has unknown role {value!r}") from None
    return frozenset(roles)


def require_platform_role(ctx: RequestContext, role: PlatformRole) -> None:
    """Enforce that ``ctx`` holds a platform role satisfying ``role`` (ADR-0043 §2).

    The sole enforcement seam **for platform roles**, orthogonal to :func:`require_role`.
    A role satisfies the requirement if it is held directly or implies it via the single
    ``platform_admin ⊇ platform_auditor`` partial order; ``platform_operator`` satisfies
    only itself.

    Raises:
        AuthorizationError: The principal holds no platform role satisfying ``role``
            (including the empty-set case — a project-only token).
    """
    for held in ctx.platform_roles:
        if held is role or role in _PLATFORM_IMPLIES.get(held, frozenset()):
            return
    raise AuthorizationError(
        f"{ctx.principal!r} needs platform role {role.value!r}; "
        f"holds {sorted(r.value for r in ctx.platform_roles)!r}"
    )
