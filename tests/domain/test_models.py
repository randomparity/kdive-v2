"""Tests for the M0 domain records (`kdive.domain.models`)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, TypedDict

import pytest
from pydantic import ValidationError

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import (
    Allocation,
    Artifact,
    DebugSession,
    ExternalRef,
    Investigation,
    Job,
    JobKind,
    Resource,
    ResourceKind,
    Run,
    Sensitivity,
    System,
)
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    JobState,
    ResourceStatus,
    RunState,
    SystemState,
)

_NOW = dt.datetime(2026, 6, 3, 12, 0, tzinfo=dt.UTC)
_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ID2 = uuid.UUID("00000000-0000-0000-0000-000000000002")


class _BaseFields(TypedDict):
    id: uuid.UUID
    created_at: dt.datetime
    updated_at: dt.datetime


class _AttribFields(TypedDict):
    principal: str
    project: str


def _base() -> _BaseFields:
    return {"id": _ID, "created_at": _NOW, "updated_at": _NOW}


def _attrib() -> _AttribFields:
    return {"principal": "alice", "project": "kernel-team"}


def test_resource_uses_status_and_carries_no_tenant_attribution() -> None:
    resource = Resource(
        **_base(),
        kind=ResourceKind.LOCAL_LIBVIRT,
        capabilities={"arch": "x86_64"},
        pool="default",
        cost_class="local",
        status=ResourceStatus.AVAILABLE,
        host_uri="qemu:///system",
    )
    assert resource.status is ResourceStatus.AVAILABLE
    assert resource.kind is ResourceKind.LOCAL_LIBVIRT
    assert "principal" not in Resource.model_fields
    assert "project" not in Resource.model_fields


def test_allocation_carries_attribution_and_optional_lease() -> None:
    allocation = Allocation(
        **_base(),
        **_attrib(),
        resource_id=_ID2,
        state=AllocationState.REQUESTED,
        capability_scope={"transports": ["gdbstub"]},
    )
    assert allocation.state is AllocationState.REQUESTED
    assert allocation.principal == "alice"
    assert allocation.agent_session is None  # M0 may run principal-only
    assert allocation.lease_expiry is None


def test_agent_session_is_carried_when_present() -> None:
    allocation = Allocation(
        **_base(),
        principal="alice",
        agent_session="sess-123",
        project="kernel-team",
        resource_id=_ID2,
        state=AllocationState.GRANTED,
    )
    assert allocation.agent_session == "sess-123"


def test_system_links_allocation_and_defaults_optional_fields() -> None:
    system = System(
        **_base(),
        **_attrib(),
        allocation_id=_ID2,
        state=SystemState.DEFINED,
        provisioning_profile={"arch": "x86_64"},
    )
    assert system.state is SystemState.DEFINED
    assert system.target_fingerprint is None
    assert system.domain_name is None


def test_investigation_external_refs_default_empty_and_accept_entries() -> None:
    bare = Investigation(
        **_base(),
        **_attrib(),
        title="oops null deref",
        state=InvestigationState.OPEN,
    )
    assert bare.external_refs == []
    assert bare.last_run_at is None

    ref = ExternalRef(tracker="bugzilla", id="BZ-42", url="https://bugs/42")
    linked = Investigation(
        **_base(),
        **_attrib(),
        title="oops null deref",
        state=InvestigationState.OPEN,
        external_refs=[ref],
    )
    assert linked.external_refs[0].tracker == "bugzilla"


def test_run_join_point_and_failure_category() -> None:
    run = Run(
        **_base(),
        **_attrib(),
        investigation_id=_ID2,
        system_id=_ID,
        state=RunState.FAILED,
        build_profile={"config": "defconfig"},
        failure_category=ErrorCategory.BUILD_FAILURE,
    )
    assert run.failure_category is ErrorCategory.BUILD_FAILURE
    assert run.kernel_ref is None
    assert run.debuginfo_ref is None


def test_debug_session_fields() -> None:
    session = DebugSession(
        **_base(),
        **_attrib(),
        run_id=_ID2,
        state=DebugSessionState.ATTACH,
        transport="gdbstub",
    )
    assert session.state is DebugSessionState.ATTACH
    assert session.transport_handle is None
    assert session.worker_heartbeat_at is None


def test_job_uses_authorizing_not_attribution_and_defaults_attempt() -> None:
    job = Job(
        **_base(),
        kind=JobKind.PROVISION,
        payload={"allocation_id": str(_ID2)},
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "project": "kernel-team"},
        dedup_key="provision:" + str(_ID2),
    )
    assert job.attempt == 0
    assert job.error_category is None
    assert "principal" not in Job.model_fields  # attribution lives in `authorizing`
    assert "authorizing" in Job.model_fields


def test_job_kind_covers_the_async_tool_surface() -> None:
    assert {kind.value for kind in JobKind} == {
        "provision",
        "teardown",
        "build",
        "install",
        "boot",
        "force_crash",
        "power",
        "capture_vmcore",
    }


def test_artifact_has_no_state_and_records_sensitivity() -> None:
    artifact = Artifact(
        **_base(),
        owner_kind="system",
        owner_id=_ID2,
        object_key="tenant/vmcore/sys/core",
        etag="abc123",
        sensitivity=Sensitivity.SENSITIVE,
        retention_class="standard",
    )
    assert artifact.sensitivity is Sensitivity.SENSITIVE
    assert "state" not in Artifact.model_fields


def test_models_reject_unknown_fields() -> None:
    # Routed through an untyped mapping: the bogus field is a runtime concern
    # (`extra="forbid"`), not a static one.
    kwargs: dict[str, Any] = {
        **_base(),
        "kind": ResourceKind.LOCAL_LIBVIRT,
        "capabilities": {},
        "pool": "default",
        "cost_class": "local",
        "status": ResourceStatus.AVAILABLE,
        "host_uri": "qemu:///system",
        "bogus_field": "nope",
    }
    with pytest.raises(ValidationError):
        Resource(**kwargs)


def test_run_round_trips_through_json() -> None:
    run = Run(
        **_base(),
        **_attrib(),
        investigation_id=_ID2,
        system_id=_ID,
        state=RunState.RUNNING,
        build_profile={"config": "defconfig"},
    )
    restored = Run.model_validate_json(run.model_dump_json())
    assert restored == run
    assert run.model_dump()["state"] == "running"
