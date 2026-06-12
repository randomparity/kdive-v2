"""Allocation request service facade tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.shapes import ResolvedSizing
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.services.allocation import request as request_service
from kdive.services.allocation.admission import AdmissionOutcome, AllocationRequest
from kdive.services.allocation.placement import PlacementCandidates, PlacementRequest
from kdive.services.allocation.request import (
    AdmissionRequestSpec,
    denial_details,
    request_admission,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_RESOURCE_ID = UUID("11111111-1111-1111-1111-111111111111")
_ALLOCATION_ID = UUID("22222222-2222-2222-2222-222222222222")
_CONN = cast(AsyncConnection, object())


def _ctx() -> RequestContext:
    return RequestContext(
        principal="user-1",
        agent_session="sess-1",
        projects=("proj",),
        roles={"proj": Role.OPERATOR},
    )


def _spec(**overrides: Any) -> AdmissionRequestSpec:
    fields: dict[str, Any] = {
        "resource_id": None,
        "kind": ResourceKind.LOCAL_LIBVIRT,
        "shape": None,
        "vcpus": 2,
        "memory_gb": 4,
        "disk_gb": 20,
        "window": 3,
        "pcie_devices": (),
        "on_capacity": "deny",
    }
    fields.update(overrides)
    return AdmissionRequestSpec(**fields)


def _sizing(**overrides: Any) -> ResolvedSizing:
    fields = {"vcpus": 2, "memory_gb": 4, "disk_gb": 20, "pcie_match": None, "shape": None}
    fields.update(overrides)
    return ResolvedSizing(**fields)


def _resource(resource_id: UUID = _RESOURCE_ID) -> Resource:
    return Resource(
        id=resource_id,
        created_at=_NOW,
        updated_at=_NOW,
        kind=ResourceKind.LOCAL_LIBVIRT,
        capabilities={"concurrent_allocation_cap": 2},
        pool="local-libvirt",
        cost_class="local",
        status=ResourceStatus.AVAILABLE,
        host_uri="qemu:///system",
    )


def _allocation(state: AllocationState = AllocationState.GRANTED) -> Allocation:
    return Allocation(
        id=_ALLOCATION_ID,
        created_at=_NOW,
        updated_at=_NOW,
        principal="user-1",
        agent_session="sess-1",
        project="proj",
        resource_id=_RESOURCE_ID,
        state=state,
    )


def test_request_admission_returns_sizing_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    error = CategorizedError("bad size", category=ErrorCategory.CONFIGURATION_ERROR)

    async def sizing_error(*_: object, **__: object) -> ResolvedSizing:
        raise error

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing_error)

    async def _run() -> None:
        result = await request_admission(_CONN, _ctx(), project="proj", spec=_spec())
        assert result.error is error
        assert result.object_id == ResourceKind.LOCAL_LIBVIRT.value

    asyncio.run(_run())


def test_request_admission_rejects_malformed_pcie_before_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing()

    async def placement(*_: object, **__: object) -> PlacementCandidates:
        raise AssertionError("malformed PCIe must reject before placement")

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)

    async def _run() -> None:
        result = await request_admission(
            _CONN,
            _ctx(),
            project="proj",
            spec=_spec(pcie_devices=("not-a-spec",)),
        )
        assert result.error is not None
        assert result.error.category is ErrorCategory.CONFIGURATION_ERROR
        assert result.error.details == {"spec": "not-a-spec"}

    asyncio.run(_run())


def test_request_admission_returns_configuration_error_when_no_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing()

    async def placement(*_: object, **__: object) -> PlacementCandidates:
        return PlacementCandidates(resources=[])

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)

    async def _run() -> None:
        result = await request_admission(_CONN, _ctx(), project="proj", spec=_spec())
        assert result.resource is None
        assert result.category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())


def test_request_admission_uses_capacity_candidate_for_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _resource()
    placement_requests: list[PlacementRequest] = []
    admission_requests: list[AllocationRequest] = []
    denial = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.ALLOCATION_DENIED,
        reason="at_capacity",
        cap=2,
        in_use=2,
        queueable=True,
    )

    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing(pcie_match="8086:1572", shape="gpu-small")

    async def placement(_conn: AsyncConnection, placement_request: object) -> PlacementCandidates:
        placement_requests.append(cast(PlacementRequest, placement_request))
        return PlacementCandidates(resources=[], capacity_candidate=resource)

    async def admit(_conn: AsyncConnection, admission_request: object) -> AdmissionOutcome:
        admission_requests.append(cast(AllocationRequest, admission_request))
        return denial

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)
    monkeypatch.setattr(request_service, "admit", admit)

    async def _run() -> None:
        result = await request_admission(
            _CONN,
            _ctx(),
            project="proj",
            spec=_spec(pcie_devices=("class=02",), on_capacity="queue"),
            idempotency_key="idem-1",
        )
        assert result.resource is resource
        assert result.denial is denial
        placement_request = placement_requests[0]
        admission_request = admission_requests[0]
        assert placement_request.pcie_specs == ("class=02", "8086:1572")
        assert admission_request.resource is resource
        assert admission_request.pcie_specs == ("class=02", "8086:1572")
        assert admission_request.shape == "gpu-small"
        assert admission_request.on_capacity == "queue"
        assert admission_request.idempotency_key == "idem-1"

    asyncio.run(_run())


def test_request_admission_returns_granted_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    resource = _resource()
    allocation = _allocation()

    async def sizing(*_: object, **__: object) -> ResolvedSizing:
        return _sizing()

    async def placement(*_: object, **__: object) -> PlacementCandidates:
        return PlacementCandidates(resources=[resource])

    async def admit(*_: object, **__: object) -> AdmissionOutcome:
        return AdmissionOutcome(granted=True, allocation=allocation)

    monkeypatch.setattr(request_service, "resolve_request_sizing", sizing)
    monkeypatch.setattr(request_service, "resolve_placement_candidates", placement)
    monkeypatch.setattr(request_service, "admit", admit)

    async def _run() -> None:
        result = await request_admission(
            _CONN,
            _ctx(),
            project="proj",
            spec=_spec(resource_id=resource.id),
        )
        assert result.object_id == str(resource.id)
        assert result.resource is resource
        assert result.allocation is allocation

    asyncio.run(_run())


def test_denial_details_copies_extra_details_and_stringifies_counts() -> None:
    outcome = AdmissionOutcome(
        granted=False,
        allocation=None,
        category=ErrorCategory.QUOTA_EXCEEDED,
        reason="quota",
        cap=2,
        in_use=1,
        details={"kind": "local-libvirt"},
    )

    assert denial_details(outcome) == {
        "kind": "local-libvirt",
        "reason": "quota",
        "cap": "2",
        "in_use": "1",
    }
