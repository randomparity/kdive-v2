"""Shared resource response envelopes for catalog reads and operator host mutations."""

from __future__ import annotations

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Resource
from kdive.mcp.responses import ToolResponse

_FLAT_CAP_KEYS = ("arch", "vcpus", "memory_mb", "concurrent_allocation_cap")


def resource_config_error(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR)


def resource_capability_data(resource: Resource) -> dict[str, str]:
    """Flatten the capabilities jsonb to string values for the envelope."""
    caps = resource.capabilities
    data: dict[str, str] = {"kind": resource.kind.value}
    for key in _FLAT_CAP_KEYS:
        if key in caps:
            data[key] = str(caps[key])
    transports = caps.get("transports")
    if isinstance(transports, (list, tuple)):
        data["transports"] = ",".join(str(t) for t in transports)
    return data


def resource_envelope(resource: Resource, *, next_actions: list[str]) -> ToolResponse:
    return ToolResponse.success(
        str(resource.id),
        resource.status.value,
        suggested_next_actions=next_actions,
        data=resource_capability_data(resource),
    )
