"""Allocation placement candidate resolution shared by request and promotion."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.repositories import RESOURCES
from kdive.domain.models import Resource, ResourceKind
from kdive.domain.pcie import MatchOutcome
from kdive.domain.state import ResourceStatus
from kdive.services.allocation import pcie_claim
from kdive.services.allocation.affinity import project_may_place


@dataclass(frozen=True, slots=True)
class PlacementRequest:
    resource_id: UUID | None
    kind: ResourceKind | None = None
    pcie_specs: tuple[str, ...] = ()
    project: str | None = None


@dataclass(frozen=True, slots=True)
class PlacementCandidates:
    resources: list[Resource]
    capacity_candidate: Resource | None = None


async def resolve_placement_candidates(
    conn: AsyncConnection, request: PlacementRequest
) -> PlacementCandidates:
    """Return schedulable placement candidates, filtered by affinity and free PCIe matches.

    Candidates are first filtered by the per-project affinity predicate (a disallowed scoped
    resource is never selected, so an any-available request falls through to a legal global
    one — ADR-0112, Task 4.2). When ``request.project`` is ``None`` no affinity filtering is
    applied. The PCIe-spec filtering then narrows the affinity-allowed set.
    """
    candidates = await _schedulable_candidates(
        conn, request.resource_id, request.kind, request.project
    )
    if not request.pcie_specs:
        return PlacementCandidates(resources=candidates)

    resources: list[Resource] = []
    capacity_candidate: Resource | None = None
    specs = list(request.pcie_specs)
    for candidate in candidates:
        descriptors = pcie_claim.descriptors_for(candidate)
        claims = await pcie_claim.active_claims(conn, candidate.id)
        resolution = pcie_claim.resolve_union(specs, descriptors, claims=claims)
        if resolution.outcome is MatchOutcome.MATCHED:
            resources.append(candidate)
        elif resolution.outcome is MatchOutcome.CAPACITY and capacity_candidate is None:
            capacity_candidate = candidate
    return PlacementCandidates(resources=resources, capacity_candidate=capacity_candidate)


async def _schedulable_candidates(
    conn: AsyncConnection,
    resource_id: UUID | None,
    kind: ResourceKind | None,
    project: str | None,
) -> list[Resource]:
    """Return schedulable candidates for either an explicit host or a resource kind.

    Candidates are filtered by the per-project affinity predicate when ``project`` is set;
    a disallowed scoped resource is excluded so it is never selected (Task 4.2). An explicit
    ``resource_id`` targeting a disallowed scoped host yields no candidate.
    """
    if resource_id is not None:
        resource = await RESOURCES.get(conn, resource_id)
        if resource is None or resource.cordoned or resource.status is not ResourceStatus.AVAILABLE:
            return []
        return [resource] if _affinity_ok(resource, project) else []
    if kind is None:
        return []
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM resources WHERE kind = %s AND status = 'available' AND NOT cordoned "
            "ORDER BY created_at, id",
            (kind.value,),
        )
        rows = await cur.fetchall()
    candidates = [Resource.model_validate(row) for row in rows]
    return [candidate for candidate in candidates if _affinity_ok(candidate, project)]


def _affinity_ok(resource: Resource, project: str | None) -> bool:
    """Apply the affinity predicate; a ``None`` project disables filtering."""
    return project is None or project_may_place(resource, project)
