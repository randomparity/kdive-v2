"""Service facade for allocation request admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.cost import Selector
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.pcie import parse_match_spec
from kdive.domain.shapes import ResolvedSizing
from kdive.security.authz.context import RequestContext
from kdive.services.allocation.admission import (
    AdmissionOutcome,
    AllocationRequest,
    admit,
)
from kdive.services.allocation.placement import PlacementRequest, resolve_placement_candidates
from kdive.services.allocation.sizing import resolve_request_sizing


@dataclass(frozen=True, slots=True)
class AdmissionRequestSpec:
    """Parsed allocation request inputs before sizing, placement, and admission."""

    resource_id: UUID | None
    kind: ResourceKind
    shape: str | None
    vcpus: int | None
    memory_gb: int | None
    disk_gb: int | None
    window: object | None
    pcie_devices: tuple[str, ...]
    on_capacity: Literal["deny", "queue"]


@dataclass(frozen=True, slots=True)
class RequestAdmissionResult:
    """Service-level allocation request outcome, ready for transport rendering."""

    object_id: str
    project: str
    resource: Resource | None = None
    allocation: Allocation | None = None
    denial: AdmissionOutcome | None = None
    error: CategorizedError | None = None
    category: ErrorCategory | None = None


async def request_admission(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    project: str,
    spec: AdmissionRequestSpec,
    idempotency_key: str | None = None,
) -> RequestAdmissionResult:
    """Resolve sizing + placement and run the shared admission gate."""
    object_id = str(spec.resource_id) if spec.resource_id is not None else spec.kind.value
    try:
        sizing = await resolve_request_sizing(
            conn,
            shape=spec.shape,
            vcpus=spec.vcpus,
            memory_gb=spec.memory_gb,
            disk_gb=spec.disk_gb,
        )
        pcie_specs = _compose_pcie_specs(spec, sizing)
    except CategorizedError as exc:
        return RequestAdmissionResult(object_id, project, error=exc)

    resource = await _select_target(conn, spec.resource_id, spec.kind, pcie_specs)
    if resource is None:
        return RequestAdmissionResult(
            object_id,
            project,
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    outcome = await admit(
        conn,
        AllocationRequest(
            ctx=ctx,
            resource=resource,
            project=project,
            selector=Selector(vcpus=sizing.vcpus, memory_gb=sizing.memory_gb),
            window=spec.window,
            idempotency_key=idempotency_key,
            disk_gb=sizing.disk_gb,
            shape=sizing.shape,
            pcie_specs=pcie_specs,
            on_capacity=spec.on_capacity,
            requested_kind=None if spec.resource_id is not None else spec.kind,
            requested_resource_id=spec.resource_id,
        ),
    )
    if outcome.granted and outcome.allocation is not None:
        return RequestAdmissionResult(
            object_id, project, resource=resource, allocation=outcome.allocation
        )
    return RequestAdmissionResult(object_id, project, resource=resource, denial=outcome)


def _compose_pcie_specs(spec: AdmissionRequestSpec, sizing: ResolvedSizing) -> tuple[str, ...]:
    """Compose and grammar-check explicit + shape-derived PCIe specs."""
    specs = spec.pcie_devices
    if sizing.pcie_match is not None:
        specs = (*specs, sizing.pcie_match)
    for pcie_spec in specs:
        parse_match_spec(pcie_spec)
    return specs


async def _select_target(
    conn: AsyncConnection,
    resource_id: UUID | None,
    kind: ResourceKind,
    specs: tuple[str, ...],
) -> Resource | None:
    """Resolve the first schedulable target, PCIe-aware when specs are present."""
    candidates = await resolve_placement_candidates(
        conn, PlacementRequest(resource_id=resource_id, kind=kind, pcie_specs=specs)
    )
    if candidates.resources:
        return candidates.resources[0]
    return candidates.capacity_candidate


def denial_details(outcome: AdmissionOutcome) -> dict[str, Any]:
    """Render admission denial details without transport-specific envelope decisions."""
    data: dict[str, Any] = dict(outcome.details)
    if outcome.reason is not None:
        data["reason"] = outcome.reason
    if outcome.cap is not None:
        data["cap"] = str(outcome.cap)
    if outcome.in_use is not None:
        data["in_use"] = str(outcome.in_use)
    return data
