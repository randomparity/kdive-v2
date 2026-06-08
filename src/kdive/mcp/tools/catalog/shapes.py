"""The shapes-catalog `shapes.*` MCP tools (M1.4 issue #160, ADR-0067).

A `viewer` read plus two `platform_operator` knobs over the `system_shapes` catalog seeded
by migration 0013 — the same authority class and audit seam as M1.3's `ops.set_host_capacity`
(ADR-0062):

* **``shapes.list()``** returns every preset, sorted by name. The catalog is shared fleet
  configuration with no ``project`` dimension, so the spec's "viewer on any project" gate
  collapses to "any authenticated context" — the ``resources.list`` precedent (ADR-0023):
  shared-infra reads need only an authenticated caller, no RBAC scoping.
* **``shapes.set(name, vcpus, memory_mb, disk_gb, pcie_match?)``** upserts a preset. A re-set
  is a full redefinition (every non-key column is rewritten). It does **not** retroactively
  re-size existing allocations/systems — those carry their own stamped sizing; a shape fixes
  the size only at request time. ``memory_mb`` is constrained to whole-GB multiples (the cost
  Selector models ``memory_gb``), and ``pcie_match`` — when given — is validated against the
  matcher grammar (ADR-0068) so a malformed spec is rejected early, never stored.
* **``shapes.delete(name)``** removes a preset. The ``shape`` name on allocations/systems is a
  **label, not an FK**, so a delete never FK-blocks and never orphans a live row; existing rows
  keep their stamped sizing. An unknown name is a ``configuration_error``.

``shapes.set`` / ``shapes.delete`` gate on :func:`require_platform_role`
(``PLATFORM_OPERATOR``); a caller lacking the role gets an ``authorization_denied`` envelope,
and the denial is audited iff the caller holds ≥1 platform role (the over-reach
accountability rule, ADR-0043 §4). Only a successful mutation writes a success audit row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field, ValidationError

from kdive.db.repositories import SYSTEM_SHAPES
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import SystemShape
from kdive.domain.pcie import parse_match_spec
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.ops._auth import audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

if TYPE_CHECKING:
    from kdive.security.authz.context import RequestContext

_LIST_TOOL = "shapes.list"
_SET_TOOL = "shapes.set"
_DELETE_TOOL = "shapes.delete"

# A curated preset name; bounded so an operator cannot seed an unwieldy catalog key (which is
# also the audit scope). Generous for any sensible preset, far below the `text` column limit.
_MAX_NAME_LEN = 64

# A placeholder satisfying the model's required `updated_at`; the DB default/trigger sets the
# real value, and the upsert excludes `updated_at` from the write (server-generated column).
_PLACEHOLDER_TS = datetime(1970, 1, 1, tzinfo=UTC)


async def list_shapes(pool: AsyncConnectionPool, ctx: RequestContext) -> ToolResponse:
    """Return every shape preset in one sorted collection envelope (viewer; no RBAC scope)."""
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            rows = await SYSTEM_SHAPES.list_all(conn)
        items = [_shape_envelope(shape) for shape in rows]
        return ToolResponse.collection(
            "shapes",
            "ok",
            items,
            suggested_next_actions=["allocations.request", _SET_TOOL],
        )


async def set_shape(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    name: str,
    vcpus: int,
    memory_mb: int,
    disk_gb: int,
    pcie_match: str | None = None,
) -> ToolResponse:
    """Upsert a shape preset (platform_operator; audited).

    A re-set fully redefines the preset and never re-sizes existing allocations/systems.
    Validates the whole-GB ``memory_mb`` constraint and the ``pcie_match`` grammar before
    persisting; a violation is a ``configuration_error`` and nothing is written.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(pool, ctx, tool=_SET_TOOL, scope=name)
            return _denied(name, _SET_TOOL)
        try:
            shape = _build_shape(name, vcpus, memory_mb, disk_gb, pcie_match)
        except CategorizedError as exc:
            return ToolResponse.failure(name, exc.category, suggested_next_actions=[_SET_TOOL])
        # `shape.name` is the canonical (stripped) key actually persisted; audit/scope on it,
        # not the raw argument, so the trail matches what the resolver will look up.
        async with pool.connection() as conn, conn.transaction():
            persisted = await SYSTEM_SHAPES.upsert(conn, shape)
            await _audit_applied(conn, ctx, _SET_TOOL, shape.name, _shape_args(persisted))
        return _shape_envelope(persisted)


async def delete_shape(
    pool: AsyncConnectionPool, ctx: RequestContext, *, name: str
) -> ToolResponse:
    """Delete a shape preset (platform_operator; audited).

    The ``shape`` label on allocations/systems is not an FK, so the delete never FK-blocks
    or orphans a live row. The name is stripped before lookup so a padded argument resolves
    the same canonical key ``set`` stores. An unknown name is a ``configuration_error`` and
    is not audited (nothing was removed).
    """
    name = name.strip()
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(pool, ctx, tool=_DELETE_TOOL, scope=name)
            return _denied(name, _DELETE_TOOL)
        async with pool.connection() as conn, conn.transaction():
            removed = await SYSTEM_SHAPES.delete(conn, name)
            if not removed:
                return ToolResponse.failure(
                    name, ErrorCategory.CONFIGURATION_ERROR, suggested_next_actions=[_LIST_TOOL]
                )
            await _audit_applied(conn, ctx, _DELETE_TOOL, name, {"name": name})
        return ToolResponse.success(name, "deleted", suggested_next_actions=[_LIST_TOOL, _SET_TOOL])


def _build_shape(
    name: str, vcpus: int, memory_mb: int, disk_gb: int, pcie_match: str | None
) -> SystemShape:
    """Validate inputs and build the candidate :class:`SystemShape` (fail-closed).

    Normalizes ``name`` by stripping surrounding whitespace so a padded name cannot create a
    shadow row the resolver can never look up. Rejects a blank or over-long name and (via the
    model) a non-positive dimension or non-whole-GB ``memory_mb`` as ``configuration_error``;
    validates ``pcie_match`` grammar before storing so a malformed spec never reaches the
    catalog. ``parse_match_spec`` already raises a ``CONFIGURATION_ERROR``
    :class:`CategorizedError` on bad grammar.
    """
    name = name.strip()
    if not name:
        raise CategorizedError(
            "shape name must be a non-blank string", category=ErrorCategory.CONFIGURATION_ERROR
        )
    if len(name) > _MAX_NAME_LEN:
        raise CategorizedError(
            f"shape name must be at most {_MAX_NAME_LEN} characters",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if pcie_match is not None:
        parse_match_spec(pcie_match)
    try:
        return SystemShape.model_validate(
            {
                "name": name,
                "vcpus": vcpus,
                "memory_mb": memory_mb,
                "disk_gb": disk_gb,
                "pcie_match": pcie_match,
                "updated_at": _PLACEHOLDER_TS,
            }
        )
    except ValidationError as exc:
        raise CategorizedError(
            f"invalid shape sizing: {exc.errors()[0]['msg']}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None


def _shape_envelope(shape: SystemShape) -> ToolResponse:
    """Build the success envelope for one shape (object_id = name)."""
    return ToolResponse.success(
        shape.name,
        "ok",
        suggested_next_actions=["allocations.request"],
        data=_shape_args(shape),
    )


def _shape_args(shape: SystemShape) -> dict[str, str]:
    """Flatten a shape to the string-valued envelope/audit payload (ADR-0019 data is str)."""
    data = {
        "name": shape.name,
        "vcpus": str(shape.vcpus),
        "memory_mb": str(shape.memory_mb),
        "disk_gb": str(shape.disk_gb),
    }
    if shape.pcie_match is not None:
        data["pcie_match"] = shape.pcie_match
    return data


async def _audit_applied(
    conn: AsyncConnection,
    ctx: RequestContext,
    tool: str,
    name: str,
    values: dict[str, str],
) -> None:
    """Write the success ``platform_audit_log`` row (scope = the shape name)."""
    await audit.record_platform(
        conn,
        principal=ctx.principal,
        agent_session=ctx.agent_session,
        event=audit.PlatformAuditEvent(
            tool=tool,
            scope=name,
            args=values,
            platform_role=held_platform_roles(ctx),
        ),
    )


def _denied(name: str, tool: str) -> ToolResponse:
    return ToolResponse.failure(
        name, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `shapes.*` catalog tools on ``app``, bound to ``pool``."""

    @app.tool(
        name=_LIST_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def shapes_list() -> ToolResponse:
        """List the named system-shape presets. Viewer."""
        return await list_shapes(pool, current_context())

    @app.tool(
        name=_SET_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def shapes_set(
        name: Annotated[str, Field(description="Shape name to upsert (e.g. 'medium').")],
        vcpus: Annotated[int, Field(description="Virtual CPU count (> 0).")],
        memory_mb: Annotated[int, Field(description="Memory in MiB, a whole-GB multiple (> 0).")],
        disk_gb: Annotated[int, Field(description="Disk size in GiB (> 0).")],
        pcie_match: Annotated[
            str | None,
            Field(
                description=(
                    "Optional PCIe match spec ('<4hex>:<4hex>' or 'class=' plus 2 or 4 hex)."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Upsert a system-shape preset. Never re-sizes existing rows. Operator."""
        return await set_shape(
            pool,
            current_context(),
            name=name,
            vcpus=vcpus,
            memory_mb=memory_mb,
            disk_gb=disk_gb,
            pcie_match=pcie_match,
        )

    @app.tool(
        name=_DELETE_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def shapes_delete(
        name: Annotated[str, Field(description="Shape name to remove.")],
    ) -> ToolResponse:
        """Delete a system-shape preset. Label-only, never FK-blocks live rows. Operator."""
        return await delete_shape(pool, current_context(), name=name)
