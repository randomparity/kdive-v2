"""The `resources.availability` fleet read (ADR-0070).

A `viewer` aggregate over the fleet: per host it reports the free capacity headroom, the
free PCIe devices, and the shapes that fit **now**; at fleet/kind granularity it reports
the pending-queue depth. It is a **point-in-time hint, never a reservation** — a host shown
free can be taken by a concurrent grant before the agent requests it; the admission/
scheduler path stays the authority (ADR-0070).

Headroom uses the **same occupancy predicate as admission** — :data:`OCCUPYING` (the
`GRANTED/ACTIVE/RELEASING` states, ADR-0069), imported from
:mod:`kdive.services.allocation.admission` so the two reads can never disagree; a queued
`requested` row holds only a queue position and is excluded. Availability is
**schedulability-aware**: a `cordoned`, non-`available`, or invalid-cap host is flagged
non-schedulable and never counts as "fits now", so the view never points the agent at a
host every request would refuse.

Resources are shared infrastructure (no `project` column), so the per-host view leaks
nothing and the read requires only an authenticated context — the `resources.list`
precedent. Queue depth is reported only as fleet/kind counts, never per project. Untrusted
host-derived PCIe labels are not returned (only the portable `bdf/vendor/device`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import SYSTEM_SHAPES
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Resource, SystemShape
from kdive.domain.pcie import (
    MatchOutcome,
    PCIeClaim,
    PCIeDescriptor,
    parse_match_spec,
    resolve_spec,
)
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.services.allocation import pcie_claim
from kdive.services.allocation.admission import OCCUPYING_VALUES

if TYPE_CHECKING:
    from kdive.security.authz.context import RequestContext

_log = logging.getLogger(__name__)

_TOOL = "resources.availability"
_REQUESTED = AllocationState.REQUESTED.value
_NEXT_ACTIONS = ["resources.describe", "allocations.request"]


class _HostAvailability(NamedTuple):
    response: ToolResponse
    fits: list[str]


def _resolve_cap(resource: Resource) -> int | None:
    """Read the per-host cap; return ``None`` for the invalid/missing case (non-raising).

    The admission gate's ``_resolve_cap`` *raises* ``configuration_error`` here — i.e. it
    treats the host as un-grantable. Availability must not crash the whole aggregate over one
    bad row, so it degrades that host to ``None`` and the caller flags it non-schedulable
    (never "fits now"), matching the admission verdict without raising.
    """
    cap = resource.capabilities.get(CONCURRENT_ALLOCATION_CAP_KEY)
    # bool is an int subclass — reject it explicitly so `True` is not read as cap 1.
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        return None
    return cap


async def _occupancy_by_resource(conn: AsyncConnection) -> dict[Any, int]:
    """Fleet-wide occupancy count per host (one query, no N+1).

    Counts the host's allocations in the shared :data:`OCCUPYING` predicate
    (GRANTED/ACTIVE/RELEASING), the same set admission's host-cap check counts; a queued
    ``requested`` row is excluded. Hosts with zero occupancy are simply absent from the map.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT resource_id, count(*) FROM allocations "
            "WHERE resource_id IS NOT NULL AND state = ANY(%s) GROUP BY resource_id",
            (OCCUPYING_VALUES,),
        )
        rows = await cur.fetchall()
    return {row[0]: int(row[1]) for row in rows}


async def _queue_depth(conn: AsyncConnection) -> dict[str, JsonValue]:
    """Report pending-queue depth at fleet/kind granularity (ADR-0070).

    A queued ``requested`` row has ``resource_id`` NULL, so it is not host-attributable. The
    fleet ``total`` is a grouping-independent count of every queued row. The ``by_kind``
    breakdown groups the by-kind rows; a **by-id** queued row carries ``requested_kind`` NULL
    (admission sets exactly one of kind/resource-id), so those land under ``by_id`` — the
    breakdown is exhaustive and the total never understated.
    """
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM allocations WHERE state = %s", (_REQUESTED,))
        total_row = await cur.fetchone()
        await cur.execute(
            "SELECT requested_kind, count(*) FROM allocations WHERE state = %s "
            "GROUP BY requested_kind",
            (_REQUESTED,),
        )
        kind_rows = await cur.fetchall()
    total = int(total_row[0]) if total_row is not None else 0
    by_kind: dict[str, int] = {}
    by_id = 0
    for kind, count in kind_rows:
        if kind is None:
            by_id += int(count)
        else:
            by_kind[str(kind)] = int(count)
    return {"total": total, "by_kind": by_kind, "by_id": by_id}


async def _claims_by_resource(conn: AsyncConnection) -> dict[Any, list[PCIeClaim]]:
    """Fleet-wide active PCIe claims grouped by host (one query, no per-host N+1).

    Unions the ``pcie_claim`` snapshots of every allocation in the shared non-terminal
    occupancy set (the same set :func:`pcie_claim.active_claims` reads per host), keyed by
    ``resource_id``. Availability is an unlocked read — unlike the in-lock claim path, it
    needs the whole fleet's occupancy in one round-trip, not one host under a held lock.
    """
    by_resource: dict[Any, list[PCIeClaim]] = {}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT resource_id, pcie_claim FROM allocations "
            "WHERE resource_id IS NOT NULL AND state = ANY(%s) AND pcie_claim <> '[]'::jsonb",
            (list(pcie_claim.NON_TERMINAL_STATES_VALUES),),
        )
        rows = await cur.fetchall()
    for resource_id, held_list in rows:
        bucket = by_resource.setdefault(resource_id, [])
        for held in held_list:
            bucket.append(
                PCIeClaim(bdf=held["bdf"], vendor_id=held["vendor_id"], device_id=held["device_id"])
            )
    return by_resource


def _free_descriptors(resource: Resource, claims: list[PCIeClaim]) -> list[PCIeDescriptor]:
    """The host's static PCIe descriptors minus the BDFs held by active claims."""
    descriptors = pcie_claim.descriptors_for(resource)
    claimed = {claim["bdf"] for claim in claims}
    return [d for d in descriptors if d["bdf"] not in claimed]


def _redact_descriptor(descriptor: PCIeDescriptor) -> dict[str, str]:
    """Project a descriptor to the portable identity; drop the untrusted host-local label."""
    return {
        "bdf": descriptor["bdf"],
        "vendor_id": descriptor["vendor_id"],
        "device_id": descriptor["device_id"],
        "class_code": descriptor["class_code"],
    }


def _shape_fits(
    shape: SystemShape, *, schedulable: bool, headroom: int, free: list[PCIeDescriptor]
) -> bool:
    """Whether ``shape`` fits NOW on this host: schedulable + headroom + a free device.

    A non-schedulable host or a host with no size headroom never fits. When the shape
    carries a ``pcie_match``, a free device must resolve for that single spec (MATCHED);
    a shape with no PCIe requirement fits on size headroom alone.
    """
    if not schedulable or headroom < 1:
        return False
    if shape.pcie_match is None:
        return True
    return resolve_spec(shape.pcie_match, free, claims=[]).outcome is MatchOutcome.MATCHED


def _host_item(
    resource: Resource,
    *,
    occupancy: int,
    free: list[PCIeDescriptor],
    shapes: list[SystemShape],
) -> _HostAvailability:
    """Build one per-host availability item (headroom, free PCIe, fitting shapes)."""
    cap = _resolve_cap(resource)
    schedulable = (
        cap is not None and resource.status is ResourceStatus.AVAILABLE and not resource.cordoned
    )
    headroom = max(cap - occupancy, 0) if cap is not None else 0
    fits = [
        shape.name
        for shape in shapes
        if _shape_fits(shape, schedulable=schedulable, headroom=headroom, free=free)
    ]
    data: dict[str, JsonValue] = {
        "kind": resource.kind.value,
        "status": resource.status.value,
        "cordoned": resource.cordoned,
        "schedulable": schedulable,
        "cap": cap if cap is not None else 0,
        "in_use": occupancy,
        "headroom": headroom,
        "free_pcie": len(free),
        "free_devices": [_redact_descriptor(d) for d in free],
        "fits": fits,
    }
    return _HostAvailability(ToolResponse.success(str(resource.id), "available", data=data), fits)


async def _resolve_shapes(conn: AsyncConnection, shape: str | None) -> list[SystemShape]:
    """The shapes to measure: the whole catalog, or the one named ``shape``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``shape`` names no catalog row.
    """
    if shape is None:
        return await SYSTEM_SHAPES.list_all(conn)
    resolved = await SYSTEM_SHAPES.get(conn, shape)
    if resolved is None:
        raise CategorizedError(
            f"system shape {shape!r} is not in the catalog",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"shape": shape},
        )
    return [resolved]


async def _fetch_resources(conn: AsyncConnection) -> list[Resource]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM resources ORDER BY created_at, id")
        rows = await cur.fetchall()
    return [Resource.model_validate(row) for row in rows]


def _passes_pcie_filter(free: list[PCIeDescriptor], pcie: str | None) -> bool:
    """Whether a host has ≥1 free device matching the optional ``pcie`` filter spec.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``pcie`` is malformed (parse path).
    """
    if pcie is None:
        return True
    return resolve_spec(pcie, free, claims=[]).outcome is MatchOutcome.MATCHED


async def availability_tool(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    pcie: str | None,
    shape: str | None,
) -> ToolResponse:
    """Report fleet availability: per-host headroom / free PCIe / fitting shapes + queue depth.

    Viewer (any authenticated context; shared infra has no project scope). ``pcie`` narrows
    to hosts with a free matching device; ``shape`` restricts the fitting computation to one
    named shape. A malformed ``pcie`` spec or an unknown ``shape`` is a
    ``configuration_error``. The view is a point-in-time hint, not a reservation (ADR-0070).
    """
    if pcie is not None:
        try:
            parse_match_spec(pcie)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(_TOOL, exc, suggested_next_actions=[_TOOL])
    # One REPEATABLE READ transaction so the whole aggregate reflects a single snapshot:
    # without it a grant committing mid-read could be counted in occupancy but its device
    # still read as free (or vice-versa). The view is a point-in-time hint either way, but a
    # self-consistent hint is the cheap, correct one.
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.transaction():
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            try:
                shapes = await _resolve_shapes(conn, shape)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(_TOOL, exc, suggested_next_actions=[_TOOL])
            resources = await _fetch_resources(conn)
            occupancy = await _occupancy_by_resource(conn)
            claims = await _claims_by_resource(conn)
            queue = await _queue_depth(conn)
        items: list[ToolResponse] = []
        global_fits: set[str] = set()
        for resource in resources:
            free = _free_descriptors(resource, claims.get(resource.id, []))
            if not _passes_pcie_filter(free, pcie):
                continue
            item = _host_item(
                resource, occupancy=occupancy.get(resource.id, 0), free=free, shapes=shapes
            )
            items.append(item.response)
            global_fits.update(item.fits)
        return ToolResponse.collection(
            "resources",
            "ok",
            items,
            suggested_next_actions=_NEXT_ACTIONS,
            data={"queue_depth": queue, "fits_now": sorted(global_fits)},
        )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `resources.availability` tool on ``app``, bound to ``pool``."""

    @app.tool(
        name=_TOOL,
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def resources_availability(
        pcie: Annotated[
            str | None,
            Field(
                description=(
                    "Optional PCIe match spec ('<4hex>:<4hex>' or 'class=' plus 2 or 4 hex); "
                    "narrows to hosts with a free matching device."
                )
            ),
        ] = None,
        shape: Annotated[
            str | None,
            Field(description="Optional shape name; restricts the fitting computation to it."),
        ] = None,
    ) -> ToolResponse:
        """Report fleet availability (headroom, free PCIe, fitting shapes, queue depth). Viewer.

        A point-in-time hint, not a reservation; the admission path stays the authority.
        """
        return await availability_tool(pool, current_context(), pcie=pcie, shape=shape)
