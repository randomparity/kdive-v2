"""Shared MCP tool-boundary helpers."""

from __future__ import annotations

from uuid import UUID

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Job
from kdive.jobs.context import authorizing, context_from_job
from kdive.mcp.responses import ResponseData, ToolResponse, current_status_data


def as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def config_error(object_id: str, *, data: ResponseData | None = None) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})


def stale_handle(object_id: str, *, current_status: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.STALE_HANDLE, data=current_status_data(current_status)
    )


def job_envelope(job: Job, object_key: str, object_id: UUID) -> ToolResponse:
    base = ToolResponse.from_job(job)
    return base.model_copy(update={"data": {**base.data, object_key: str(object_id)}})


__all__ = [
    "as_uuid",
    "authorizing",
    "config_error",
    "context_from_job",
    "job_envelope",
    "stale_handle",
]
