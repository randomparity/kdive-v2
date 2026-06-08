"""The uniform tool-response envelope every MCP tool returns (ADR-0019).

Every tool — across all planes — returns a :class:`ToolResponse` carrying the
object id, a status, literal next tool names, artifact references, and (only for a
failure) an error category. The shape is fixed surface-wide so an agent learns one
envelope and one polling pattern, and so "references, never log dumps" is structural
rather than per-plane discipline.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, model_validator

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Job
from kdive.domain.state import JobState

# Literal next tool names by the job's state. Only `jobs.*` exist in M0; the
# artifact-retrieval action joins the succeeded row when `artifacts.get` ships (#19).
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


class ToolResponse(BaseModel):
    """The structured JSON every MCP tool returns (ADR-0019)."""

    object_id: str
    status: str
    suggested_next_actions: list[str] = []
    refs: dict[str, str] = {}
    error_category: str | None = None
    data: dict[str, str] = {}

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
        data: dict[str, str] | None = None,
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
            data=data or {},
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
        data: dict[str, str] | None = None,
    ) -> ToolResponse:
        """Build one envelope for a collection-returning tool."""
        payload = dict(data or {})
        payload["items"] = json.dumps(
            [item.model_dump(mode="json") for item in items],
            sort_keys=True,
        )
        payload["count"] = str(len(items))
        return cls.success(
            object_id,
            status,
            suggested_next_actions=suggested_next_actions,
            refs=refs,
            data=payload,
        )

    def collection_items(self) -> list[ToolResponse]:
        """Parse ``data["items"]`` from a collection envelope."""
        raw = self.data.get("items", "[]")
        decoded = json.loads(raw)
        if not isinstance(decoded, list):
            raise ValueError("collection envelope data['items'] must be a JSON list")
        return [ToolResponse.model_validate(item) for item in decoded]

    @classmethod
    def failure(
        cls,
        object_id: str,
        category: ErrorCategory,
        *,
        suggested_next_actions: list[str] | None = None,
        data: dict[str, str] | None = None,
    ) -> ToolResponse:
        """Build a tool-level failure envelope (``status="error"`` + ``category``)."""
        return cls(
            object_id=object_id,
            status="error",
            error_category=category.value,
            suggested_next_actions=suggested_next_actions or [],
            data=data or {},
        )

    @classmethod
    def from_job(cls, job: Job) -> ToolResponse:
        """Build the job-handle envelope from a :class:`Job` row."""
        refs = {"result": job.result_ref} if job.result_ref else {}
        data = {"kind": job.kind.value}
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
