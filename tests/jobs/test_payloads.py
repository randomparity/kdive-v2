"""Tests for typed job payload contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs.payloads import (
    BuildPayload,
    PayloadValidationError,
    ReprovisionPayload,
    dump_authorizing,
    dump_payload,
    load_payload,
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


def test_reprovision_payload_includes_profile_digest() -> None:
    system_id = uuid4()
    payload = dump_payload(
        JobKind.REPROVISION,
        {"system_id": str(system_id), "profile_digest": "abc123"},
    )

    decoded = ReprovisionPayload.model_validate(payload)

    assert decoded.system_id == str(system_id)
    assert decoded.profile_digest == "abc123"


def test_authorizing_requires_project_at_enqueue_boundary() -> None:
    auth = dump_authorizing(
        {"principal": "alice", "agent_session": "sess-1", "project": "kernel-team"}
    )

    assert auth == {
        "principal": "alice",
        "agent_session": "sess-1",
        "project": "kernel-team",
    }


def test_authorizing_rejects_missing_project() -> None:
    with pytest.raises(PayloadValidationError, match="invalid job authorizing"):
        dump_authorizing({"principal": "alice"})
