"""The three-check destructive-op gate (ADR-0006, ADR-0020).

A destructive operation is allowed only when all three independent checks pass: the
allocation's capability scope grants the op, the principal holds `admin` on the
allocation's project, and the controlling profile explicitly opted the op in. The gate
is pure policy over `(ctx, allocation, op)`; it reads the first two checks from data
and trusts the handler to resolve the third (`profile_opt_in`). A denial raises
`DestructiveOpDenied` listing every missing check, so an audit/log line shows the full
reason. The gate never writes audit rows (it has no connection); a handler that catches
`DestructiveOpDenied` audits the denied attempt with `transition=f"{op.kind}:denied"`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kdive.security.rbac import AuthorizationError, Role, require_role

if TYPE_CHECKING:
    from kdive.domain.models import Allocation
    from kdive.mcp.auth import RequestContext

_DESTRUCTIVE_OPS_KEY = "destructive_ops"


@dataclass(frozen=True)
class DestructiveOp:
    """A destructive operation and whether its controlling profile opted it in.

    ``profile_opt_in`` defaults to ``False`` so a handler that forgets to resolve the
    opt-in is denied (deny-by-default).
    """

    kind: str
    profile_opt_in: bool = False


class DestructiveOpDenied(AuthorizationError):
    """A destructive op failed one or more of the three gate checks."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"destructive op denied; missing checks: {missing}")


def _scope_permits(allocation: Allocation, op: DestructiveOp) -> bool:
    granted = allocation.capability_scope.get(_DESTRUCTIVE_OPS_KEY)
    return isinstance(granted, (list, tuple)) and op.kind in granted


def assert_destructive_allowed(
    ctx: RequestContext, allocation: Allocation, op: DestructiveOp
) -> None:
    """Allow a destructive op only if all three checks pass.

    Raises:
        DestructiveOpDenied: One or more of capability scope, ``admin`` role, or profile
            opt-in is absent; ``.missing`` lists every failed check in check order.
    """
    missing: list[str] = []
    if not _scope_permits(allocation, op):
        missing.append("capability_scope")
    try:
        require_role(ctx, allocation.project, Role.ADMIN)
    except AuthorizationError:
        missing.append("admin_role")
    if not op.profile_opt_in:
        missing.append("profile_opt_in")
    if missing:
        raise DestructiveOpDenied(missing)
