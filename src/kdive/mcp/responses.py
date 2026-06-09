"""The uniform tool-response envelope every MCP tool returns (ADR-0019).

Every tool — across all planes — returns a :class:`ToolResponse` carrying the
object id, a status, literal next tool names, artifact references, and (only for a
failure) an error category. The shape is fixed surface-wide so an agent learns one
envelope and one polling pattern, and so "references, never log dumps" is structural
rather than per-plane discipline.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job
from kdive.domain.state import JobState

# Literal next tool names by the job's state.
# See the design doc's suggested_next_actions table.
_NEXT_ACTIONS: dict[JobState, list[str]] = {
    JobState.QUEUED: ["jobs.wait", "jobs.cancel"],
    JobState.RUNNING: ["jobs.wait", "jobs.cancel"],
    JobState.SUCCEEDED: ["jobs.get"],
    JobState.FAILED: ["jobs.get"],
    JobState.CANCELED: [],
}

# A response reports a failure under the job's `failed` lifecycle status or the
# tool-level `error` status; both require an error category, all others forbid one.
_FAILURE_STATUSES = frozenset({JobState.FAILED.value, "error"})

JsonValue = str | int | float | bool | None | list[object] | dict[str, object]
ResponseData = Mapping[str, object]


def _validate_json_value(value: object, *, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must be finite JSON number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} object keys must be strings")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise ValueError(f"{path} contains non-JSON value {type(value).__name__}")


def current_status_data(current_status: str) -> dict[str, JsonValue]:
    return {"current_status": current_status}


def reason_data(reason: str) -> dict[str, JsonValue]:
    return {"reason": reason}


def _safe_error_details(details: Mapping[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in details.items():
        if isinstance(value, float):
            if math.isfinite(value):
                safe[key] = value
            continue
        if isinstance(value, (str, bool, int)):
            safe[key] = value
    return safe


class ToolResponse(BaseModel):
    """The structured JSON every MCP tool returns (ADR-0019)."""

    object_id: str
    status: str
    suggested_next_actions: list[str] = Field(default_factory=list)
    refs: dict[str, str] = Field(default_factory=dict)
    error_category: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    items: list[ToolResponse] = Field(default_factory=list)

    @field_validator("data")
    @classmethod
    def _data_is_json_compatible(cls, data: dict[str, Any]) -> dict[str, Any]:
        for key, value in data.items():
            _validate_json_value(value, path=f"data.{key}")
        return data

    @model_validator(mode="after")
    def _category_iff_failed(self) -> ToolResponse:
        """Enforce: ``error_category`` is set iff the object is in a failure status.

        A failure status without a category, or any other status carrying one, is a
        producer bug — fail fast at construction (ADR-0019). Batch callers
        (``*.list``) isolate this per row so one bad object cannot blank a list.
        """
        is_failure = self.status in _FAILURE_STATUSES
        if is_failure and self.error_category is None:
            raise ValueError(f"status {self.status!r} requires an error_category")
        if not is_failure and self.error_category is not None:
            raise ValueError(f"error_category set on non-failure status {self.status!r}")
        return self

    @classmethod
    def success(
        cls,
        object_id: str,
        status: str,
        *,
        suggested_next_actions: list[str] | None = None,
        refs: dict[str, str] | None = None,
        data: ResponseData | None = None,
    ) -> ToolResponse:
        """Build a non-failure envelope.

        ``status`` must not be a failure status (``failed``/``error``); passing one is a
        producer bug and the model validator raises, surfacing the misuse at construction.
        """
        return cls(
            object_id=object_id,
            status=status,
            suggested_next_actions=suggested_next_actions or [],
            refs=refs or {},
            data=dict(data or {}),
        )

    @classmethod
    def collection(
        cls,
        object_id: str,
        status: str,
        items: list[ToolResponse],
        *,
        suggested_next_actions: list[str] | None = None,
        refs: dict[str, str] | None = None,
        data: ResponseData | None = None,
    ) -> ToolResponse:
        """Build one envelope for a collection-returning tool."""
        payload = dict(data or {})
        payload["count"] = str(len(items))
        return cls(
            object_id=object_id,
            status=status,
            suggested_next_actions=suggested_next_actions or [],
            refs=refs or {},
            data=payload,
            items=list(items),
        )

    @classmethod
    def failure(
        cls,
        object_id: str,
        category: ErrorCategory,
        *,
        suggested_next_actions: list[str] | None = None,
        data: ResponseData | None = None,
    ) -> ToolResponse:
        return cls(
            object_id=object_id,
            status="error",
            error_category=category.value,
            suggested_next_actions=suggested_next_actions or [],
            data=dict(data or {}),
        )

    @classmethod
    def failure_from_error(
        cls,
        object_id: str,
        exc: CategorizedError,
        *,
        category: ErrorCategory | None = None,
        suggested_next_actions: list[str] | None = None,
        data: ResponseData | None = None,
    ) -> ToolResponse:
        payload: dict[str, object] = _safe_error_details(exc.details)
        payload.update(data or {})
        return cls.failure(
            object_id,
            category or exc.category,
            suggested_next_actions=suggested_next_actions,
            data=payload,
        )

    @classmethod
    def from_job(cls, job: Job) -> ToolResponse:
        refs = {"result": job.result_ref} if job.result_ref else {}
        data: dict[str, JsonValue] = {"kind": job.kind.value}
        if job.state is JobState.FAILED:
            data.update(job.failure_context)
        return cls(
            object_id=str(job.id),
            status=job.state.value,
            suggested_next_actions=list(_NEXT_ACTIONS[job.state]),
            refs=refs,
            error_category=job.error_category.value if job.error_category else None,
            data=data,
        )
