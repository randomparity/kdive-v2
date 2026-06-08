"""Shared helpers for lifecycle run MCP tool lanes."""

from __future__ import annotations

from uuid import UUID

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Job, Run
from kdive.domain.state import AllocationState, InvestigationState, RunState, SystemState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import job_envelope

RUN_HOSTABLE = frozenset({SystemState.READY})
SYSTEM_GONE = frozenset({SystemState.TORN_DOWN, SystemState.FAILED, SystemState.CRASHED})
ALLOC_HOSTABLE = frozenset({AllocationState.ACTIVE})
INVESTIGATION_OPEN_FOR_RUN = frozenset({InvestigationState.OPEN, InvestigationState.ACTIVE})
RUN_BUILD_TERMINAL = frozenset({RunState.FAILED, RunState.CANCELED})


def envelope_for_run(run: Run, *, required_cmdline: str | None = None) -> ToolResponse:
    """Render a Run; `failed` becomes a failure envelope carrying its `failure_category`."""
    if run.state is RunState.FAILED:
        category = run.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return ToolResponse.failure(str(run.id), category, data={"current_status": run.state.value})
    if run.state in (RunState.CREATED, RunState.RUNNING):
        actions = ["runs.get", "runs.build"]
    else:
        actions = ["runs.get"]
    data: dict[str, object] = {"project": run.project}
    if required_cmdline is not None:
        data["required_cmdline"] = required_cmdline
    if run.expected_boot_failure is not None:
        kind = run.expected_boot_failure.get("kind")
        if isinstance(kind, str):
            data["expected_boot_failure"] = kind
        data["expected_boot_failure_detail"] = run.expected_boot_failure
    return ToolResponse.success(
        str(run.id), run.state.value, suggested_next_actions=actions, data=data
    )


def run_job_envelope(job: Job, run_id: UUID) -> ToolResponse:
    """Render a run-scoped job envelope."""
    return job_envelope(job, "run_id", run_id)
