"""Runtime capacity/cost tuning `ops.*` MCP tools (M1.3 issue 5, ADR-0062 §4).

Two `platform_operator` knobs, both audited to ``platform_audit_log``:

* **``ops.set_cost_class_coeff(cost_class, coeff)``** upserts the ``cost_class_coefficients``
  row the pricing path reads (``domain/cost.py``). New pricing applies from the **next**
  charge onward; it never retro-reprices committed ledger rows. The pricing read is already
  DB-backed and fail-closed on a missing row, so this is a direct upsert.
* **``ops.set_host_capacity(resource_id, concurrent_allocation_cap)``** updates the host's
  ``concurrent_allocation_cap`` inside its ``capabilities`` jsonb — the value admission's
  per-host cap reads (``services/allocation_admission.py``). Lowering the cap below the live
  non-terminal count does **not** evict anyone; it blocks *new* placement until the count
  falls (admission only ever compares the live count against the cap at request time).

Both gate on :func:`require_platform_role` (``PLATFORM_OPERATOR``); a caller lacking the
role gets an ``authorization_denied`` envelope (the denial is audited iff the caller holds
≥1 platform role, mirroring ``accounting.report``'s all-projects SoD-denial rule).
"""

from __future__ import annotations

from decimal import Decimal, DecimalException, InvalidOperation
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.security import audit
from kdive.security.rbac import AuthorizationError, PlatformRole, require_platform_role

if TYPE_CHECKING:
    from kdive.security.context import RequestContext

_COEFF_OBJECT_ID = "cost_class_coefficient"
_CAPACITY_OBJECT_ID = "host_capacity"
_SET_COEFF_TOOL = "ops.set_cost_class_coeff"
_SET_CAPACITY_TOOL = "ops.set_host_capacity"


async def set_cost_class_coeff(
    pool: AsyncConnectionPool, ctx: RequestContext, *, cost_class: str, coeff: object
) -> ToolResponse:
    """Upsert a ``cost_class → coeff`` pricing row (platform_operator; audited).

    The new coefficient prices charges from the next charge onward; committed ledger rows
    are unchanged (no retro-reprice). Fails closed on a non-finite/negative coefficient.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await _audit_denial(pool, ctx, _SET_COEFF_TOOL, cost_class)
            return _denied(_COEFF_OBJECT_ID, _SET_COEFF_TOOL)
        try:
            _validate_cost_class(cost_class)
            parsed = _parse_positive_coeff(coeff)
        except CategorizedError as exc:
            return ToolResponse.failure(
                _COEFF_OBJECT_ID, exc.category, suggested_next_actions=[_SET_COEFF_TOOL]
            )
        async with pool.connection() as conn, conn.transaction():
            await _upsert_coeff(conn, cost_class, parsed)
            await _audit_applied(conn, ctx, _SET_COEFF_TOOL, cost_class, {"coeff": str(parsed)})
        return ToolResponse.success(
            _COEFF_OBJECT_ID,
            "ok",
            suggested_next_actions=["accounting.estimate", "allocations.request"],
            data={"cost_class": cost_class, "coeff": str(parsed)},
        )


async def set_host_capacity(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    resource_id: str,
    concurrent_allocation_cap: int,
) -> ToolResponse:
    """Set a host's ``concurrent_allocation_cap`` in its capabilities (platform_operator).

    Admission honors the new cap on the next request. Lowering it below the live count
    blocks new placement without evicting anyone. Audited to ``platform_audit_log``.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await _audit_denial(pool, ctx, _SET_CAPACITY_TOOL, resource_id)
            return _denied(_CAPACITY_OBJECT_ID, _SET_CAPACITY_TOOL)
        try:
            target = _parse_resource_id(resource_id)
            cap = _parse_cap(concurrent_allocation_cap)
        except CategorizedError as exc:
            return ToolResponse.failure(
                _CAPACITY_OBJECT_ID, exc.category, suggested_next_actions=[_SET_CAPACITY_TOOL]
            )
        async with pool.connection() as conn, conn.transaction():
            updated = await _update_host_cap(conn, target, cap)
            if not updated:
                return ToolResponse.failure(
                    _CAPACITY_OBJECT_ID,
                    ErrorCategory.CONFIGURATION_ERROR,
                    suggested_next_actions=["resources.list"],
                )
            await _audit_applied(
                conn,
                ctx,
                _SET_CAPACITY_TOOL,
                resource_id,
                {CONCURRENT_ALLOCATION_CAP_KEY: str(cap)},
            )
        return ToolResponse.success(
            _CAPACITY_OBJECT_ID,
            "ok",
            suggested_next_actions=["resources.list", "allocations.request"],
            data={"resource_id": resource_id, CONCURRENT_ALLOCATION_CAP_KEY: str(cap)},
        )


def _validate_cost_class(cost_class: str) -> None:
    """Reject a blank cost class (fail closed); an empty key would seed unreachable junk."""
    if not cost_class.strip():
        raise CategorizedError(
            "cost_class must be a non-blank string",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def _parse_positive_coeff(value: object) -> Decimal:
    """Parse ``value`` into a finite, positive coefficient (fail closed).

    A coefficient is a price multiplier; ``0`` or negative would price work as free or as a
    budget credit, so both are rejected as ``configuration_error`` (the same fail-closed
    discipline ``domain/cost.py`` applies to the row).
    """
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, DecimalException, ValueError, TypeError):
        raise CategorizedError(
            f"coeff {value!r} is not a number", category=ErrorCategory.CONFIGURATION_ERROR
        ) from None
    if not parsed.is_finite() or parsed <= 0:
        raise CategorizedError(
            f"coeff {value!r} must be a finite number > 0",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return parsed


def _parse_cap(value: int) -> int:
    """Reject a negative cap (fail closed); ``0`` is a valid full freeze on new placement."""
    if value < 0:
        raise CategorizedError(
            f"{CONCURRENT_ALLOCATION_CAP_KEY} {value} must be >= 0",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return value


def _parse_resource_id(value: str) -> UUID:
    """Parse the host id, rejecting a malformed UUID as ``configuration_error``."""
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise CategorizedError(
            f"resource_id {value!r} is not a UUID",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None


async def _upsert_coeff(conn: AsyncConnection, cost_class: str, coeff: Decimal) -> None:
    """Upsert the ``cost_class_coefficients`` row; the updated_at trigger stamps it."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO cost_class_coefficients (cost_class, coeff) VALUES (%s, %s) "
            "ON CONFLICT (cost_class) DO UPDATE SET coeff = EXCLUDED.coeff",
            (cost_class, coeff),
        )


async def _update_host_cap(conn: AsyncConnection, resource_id: UUID, cap: int) -> bool:
    """Merge ``concurrent_allocation_cap`` into the host's capabilities jsonb in place.

    A targeted ``jsonb ||`` merge (not a whole-model upsert) so a concurrent status/health
    write cannot be clobbered by a stale read, and the rest of the host's capabilities are
    preserved. Returns ``False`` if no host has that id.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE resources "
            "SET capabilities = capabilities || jsonb_build_object(%s::text, %s::int) "
            "WHERE id = %s",
            (CONCURRENT_ALLOCATION_CAP_KEY, cap, resource_id),
        )
        return cur.rowcount == 1


async def _audit_applied(
    conn: AsyncConnection,
    ctx: RequestContext,
    tool: str,
    target: str,
    values: dict[str, str],
) -> None:
    """Write the success ``platform_audit_log`` row (scope = the tuned target)."""
    await audit.record_platform(
        conn,
        principal=ctx.principal,
        agent_session=ctx.agent_session,
        event=audit.PlatformAuditEvent(
            tool=tool,
            scope=target,
            args=values,
            platform_role=_held_platform_roles(ctx),
        ),
    )


async def _audit_denial(
    pool: AsyncConnectionPool, ctx: RequestContext, tool: str, target: str
) -> None:
    """Audit a denial iff the caller holds ≥1 platform role (ADR-0043 §4 amplification rule).

    A project-only token's denial is the routine non-grant case and is not recorded;
    recording it would let any authenticated token amplify writes into ``platform_audit_log``.
    The role check ran before any connection opened, so this opens its own connection.
    """
    held = _held_platform_roles(ctx)
    if held is None:
        return
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(tool=tool, scope=target, args={}, platform_role=held),
        )


def _held_platform_roles(ctx: RequestContext) -> str | None:
    """Return the caller's platform roles as a sorted comma string, or None if it holds none."""
    if not ctx.platform_roles:
        return None
    return ",".join(sorted(r.value for r in ctx.platform_roles))


def _denied(object_id: str, tool: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the runtime-tuning `ops.*` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name=_SET_COEFF_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def ops_set_cost_class_coeff(
        cost_class: Annotated[
            str, Field(description="Cost class whose pricing coefficient to set.")
        ],
        coeff: Annotated[
            float | str,
            Field(description="Pricing multiplier (number or decimal string, > 0)."),
        ],
    ) -> ToolResponse:
        """Upsert a cost class's pricing coefficient. Applies from the next charge. Operator."""
        return await set_cost_class_coeff(
            pool, current_context(), cost_class=cost_class, coeff=coeff
        )

    @app.tool(
        name=_SET_CAPACITY_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def ops_set_host_capacity(
        resource_id: Annotated[str, Field(description="Host (resource) id to set the cap for.")],
        concurrent_allocation_cap: Annotated[
            int, Field(description="Maximum concurrent allocations on the host (>= 0).")
        ],
    ) -> ToolResponse:
        """Set a host's concurrent allocation cap. Blocks new placement; no eviction. Operator."""
        return await set_host_capacity(
            pool,
            current_context(),
            resource_id=resource_id,
            concurrent_allocation_cap=concurrent_allocation_cap,
        )
