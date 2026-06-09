"""Tests for typed job payload contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import Job, JobKind, PowerAction
from kdive.domain.state import JobState
from kdive.jobs.payloads import (
    Authorizing,
    BuildPayload,
    CaptureVmcorePayload,
    PayloadValidationError,
    PowerPayload,
    ReprovisionPayload,
    dump_authorizing,
    dump_payload,
    load_payload,
    run_id_from_payload,
)


def test_build_payload_round_trips_with_optional_cmdline() -> None:
    run_id = uuid4()
    now = datetime.now(UTC)

    payload = dump_payload(JobKind.BUILD, {"run_id": str(run_id), "cmdline": "panic=1"})
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.BUILD,
        payload=payload,
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key="build",
    )
    decoded = load_payload(job, BuildPayload)

    assert payload == {"run_id": str(run_id), "cmdline": "panic=1"}
    assert decoded.run_id == str(run_id)
    assert decoded.cmdline == "panic=1"


def test_payload_validation_rejects_wrong_shape_for_kind() -> None:
    with pytest.raises(PayloadValidationError, match="invalid build payload"):
        dump_payload(JobKind.BUILD, {"system_id": str(uuid4())})


def test_run_id_from_payload_returns_uuid_for_run_jobs() -> None:
    run_id = uuid4()

    assert run_id_from_payload(JobKind.BUILD, {"run_id": str(run_id)}) == run_id
    assert run_id_from_payload(JobKind.INSTALL, {"run_id": str(run_id)}) == run_id
    assert run_id_from_payload(JobKind.BOOT, {"run_id": str(run_id)}) == run_id


def test_run_id_from_payload_returns_none_for_system_jobs() -> None:
    assert run_id_from_payload(JobKind.PROVISION, {"system_id": str(uuid4())}) is None


def test_run_id_from_payload_rejects_malformed_run_jobs() -> None:
    with pytest.raises(PayloadValidationError, match="invalid build payload"):
        run_id_from_payload(JobKind.BUILD, {"run_id": "not-a-uuid"})


def test_reprovision_payload_includes_profile_digest() -> None:
    system_id = uuid4()
    payload = dump_payload(
        JobKind.REPROVISION,
        {"system_id": str(system_id), "profile_digest": "abc123"},
    )

    decoded = ReprovisionPayload.model_validate(payload)

    assert decoded.system_id == str(system_id)
    assert decoded.profile_digest == "abc123"


def test_capture_payload_dumps_json_and_loads_enum() -> None:
    system_id = uuid4()
    now = datetime.now(UTC)

    payload = dump_payload(
        JobKind.CAPTURE_VMCORE,
        {"system_id": str(system_id), "method": "host_dump"},
    )
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.CAPTURE_VMCORE,
        payload=payload,
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key="capture",
    )

    decoded = load_payload(job, CaptureVmcorePayload)

    assert payload == {"system_id": str(system_id), "method": "host_dump"}
    assert decoded.method is CaptureMethod.HOST_DUMP


def test_power_payload_dumps_json_and_loads_enum() -> None:
    system_id = uuid4()
    now = datetime.now(UTC)

    payload = dump_payload(JobKind.POWER, {"system_id": str(system_id), "action": "reset"})
    job = Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=JobKind.POWER,
        payload=payload,
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key="power",
    )

    decoded = load_payload(job, PowerPayload)

    assert payload == {"system_id": str(system_id), "action": "reset"}
    assert decoded.action is PowerAction.RESET


def test_authorizing_requires_project_at_enqueue_boundary() -> None:
    auth = dump_authorizing(
        Authorizing(principal="alice", agent_session="sess-1", project="kernel-team")
    )

    assert auth == {
        "principal": "alice",
        "agent_session": "sess-1",
        "project": "kernel-team",
    }


def test_authorizing_rejects_missing_project() -> None:
    with pytest.raises(PayloadValidationError, match="invalid job authorizing"):
        dump_authorizing(cast(Any, {"principal": "alice"}))
