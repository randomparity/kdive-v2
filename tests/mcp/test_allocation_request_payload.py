"""Shape XOR full-custom-triple validation on the allocation request payload (#161).

A request names a shape NAME or a full custom ``{vcpus, memory_gb, disk_gb}`` triple, never
both and never neither. A partial custom triple (e.g. missing ``disk_gb``) is rejected at
this boundary as a structural error, so it can never reach a NULL ``requested_disk_gb``
snapshot that would silently disable the size unification (ADR-0067).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kdive.mcp.tool_payloads import AllocationRequestPayload, EstimateRequestPayload


def test_shape_only_is_valid() -> None:
    payload = AllocationRequestPayload.model_validate({"shape": "medium"})
    assert payload.shape == "medium"
    assert payload.vcpus is None
    assert payload.memory_gb is None
    assert payload.disk_gb is None


def test_full_custom_triple_is_valid() -> None:
    payload = AllocationRequestPayload.model_validate({"vcpus": 2, "memory_gb": 4, "disk_gb": 20})
    assert payload.shape is None
    assert (payload.vcpus, payload.memory_gb, payload.disk_gb) == (2, 4, 20)


def test_shape_and_custom_together_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AllocationRequestPayload.model_validate(
            {"shape": "medium", "vcpus": 2, "memory_gb": 4, "disk_gb": 20}
        )


def test_neither_shape_nor_custom_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AllocationRequestPayload.model_validate({})


@pytest.mark.parametrize(
    "partial",
    [
        {"vcpus": 2, "memory_gb": 4},
        {"vcpus": 2, "disk_gb": 20},
        {"memory_gb": 4, "disk_gb": 20},
        {"vcpus": 2},
        {"disk_gb": 20},
    ],
)
def test_partial_custom_triple_is_rejected(partial: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        AllocationRequestPayload.model_validate(partial)


def test_shape_with_one_custom_field_is_rejected() -> None:
    # A shape plus any custom sizing field is "both sides set" — rejected.
    with pytest.raises(ValidationError):
        AllocationRequestPayload.model_validate({"shape": "small", "vcpus": 2})


def test_estimate_payload_keeps_custom_only_sizing() -> None:
    # Estimate is a read-side custom-only price; it never gained shapes (ADR-0067), so its
    # vcpus/memory_gb stay required and it has no shape/disk_gb fields.
    payload = EstimateRequestPayload.model_validate({"vcpus": 1, "memory_gb": 2, "window": 1})
    assert (payload.vcpus, payload.memory_gb) == (1, 2)
    assert "shape" not in EstimateRequestPayload.model_fields
    with pytest.raises(ValidationError):
        EstimateRequestPayload.model_validate({"window": 1})
