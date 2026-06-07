"""Tests for queued-job attribution helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from kdive.domain.models import Job, JobKind
from kdive.domain.state import JobState
from kdive.jobs.context import authorizing, context_from_job
from kdive.security.context import RequestContext

_NOW = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)


def _job(*, principal: str, agent_session: str | None, project: str) -> Job:
    return Job(
        id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        kind=JobKind.BUILD,
        payload={"run_id": str(uuid4())},
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={
            "principal": principal,
            "agent_session": agent_session,
            "project": project,
        },
        dedup_key=str(uuid4()),
    )


def test_authorizing_serializes_request_principal_session_and_project() -> None:
    ctx = RequestContext(
        principal="alice",
        agent_session="sess-1",
        projects=("kernel-team", "other"),
        roles={},
    )

    auth = authorizing(ctx, "kernel-team")

    assert auth.principal == "alice"
    assert auth.agent_session == "sess-1"
    assert auth.project == "kernel-team"


def test_context_from_job_reconstructs_attribution_without_role_grants() -> None:
    job = _job(principal="worker-user", agent_session="agent-123", project="stored-project")

    ctx = context_from_job(job, "effective-project")

    assert ctx.principal == "worker-user"
    assert ctx.agent_session == "agent-123"
    assert ctx.projects == ("effective-project",)
    assert ctx.roles == {}
    assert ctx.platform_roles == frozenset()
