"""ToolResponse envelope tests (ADR-0019) — pure, no DB."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.mcp.responses import ToolResponse

_NOW = dt.datetime(2026, 6, 3, 12, 0, tzinfo=dt.UTC)


def _job(
    state: JobState,
    *,
    result_ref: str | None = None,
    error_category: ErrorCategory | None = None,
) -> Job:
    return Job(
        id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        kind=JobKind.BUILD,
        payload={},
        state=state,
        max_attempts=3,
        result_ref=result_ref,
        error_category=error_category,
        authorizing={"principal": "p"},
        dedup_key=str(uuid4()),
    )


def test_from_job_running_has_no_refs_and_polling_actions() -> None:
    job = _job(JobState.RUNNING)
    resp = ToolResponse.from_job(job)
    assert resp.object_id == str(job.id)
    assert resp.status == "running"
    assert resp.data == {"kind": "build"}
    assert resp.refs == {}
    assert resp.error_category is None
    assert resp.suggested_next_actions == ["jobs.wait", "jobs.cancel"]


def test_from_job_succeeded_exposes_result_ref() -> None:
    job = _job(JobState.SUCCEEDED, result_ref="tenant/run/abc/kernel")
    resp = ToolResponse.from_job(job)
    assert resp.status == "succeeded"
    assert resp.refs == {"result": "tenant/run/abc/kernel"}
    assert resp.suggested_next_actions == ["jobs.get"]


def test_from_job_failed_carries_category() -> None:
    job = _job(JobState.FAILED, error_category=ErrorCategory.BUILD_FAILURE)
    resp = ToolResponse.from_job(job)
    assert resp.status == "failed"
    assert resp.error_category == "build_failure"
    assert resp.suggested_next_actions == ["jobs.get"]


def test_from_job_canceled_has_no_actions() -> None:
    resp = ToolResponse.from_job(_job(JobState.CANCELED))
    assert resp.status == "canceled"
    assert resp.suggested_next_actions == []


def test_category_without_failure_is_rejected() -> None:
    with pytest.raises(ValueError, match="error_category"):
        ToolResponse(object_id="x", status="running", error_category="build_failure")


def test_failure_without_category_is_rejected() -> None:
    # The validator treats status in {"failed", "error"} as a failure status, which
    # therefore requires a category.
    with pytest.raises(ValueError, match="error_category"):
        ToolResponse(object_id="x", status="error", error_category=None)


def test_success_factory_builds_non_failure_envelope() -> None:
    resp = ToolResponse.success(
        "alloc-1", "granted", suggested_next_actions=["allocations.release"], data={"k": "v"}
    )
    assert resp.object_id == "alloc-1"
    assert resp.status == "granted"
    assert resp.error_category is None
    assert resp.suggested_next_actions == ["allocations.release"]
    assert resp.data == {"k": "v"}


def test_success_factory_on_failure_status_raises() -> None:
    # "failed" is a failure status; building it via success() (no category) is misuse.
    with pytest.raises(ValueError, match="error_category"):
        ToolResponse.success("alloc-1", "failed")


def test_failure_factory_sets_error_status_and_category() -> None:
    resp = ToolResponse.failure(
        "res-1", ErrorCategory.ALLOCATION_DENIED, data={"reason": "at_capacity"}
    )
    assert resp.status == "error"
    assert resp.error_category == "allocation_denied"
    assert resp.data == {"reason": "at_capacity"}
    assert resp.suggested_next_actions == []
