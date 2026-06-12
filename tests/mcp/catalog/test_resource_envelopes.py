"""Shared resource envelope helper tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Resource, ResourceKind
from kdive.domain.state import ResourceStatus
from kdive.mcp.tools._resource_envelopes import (
    resource_capability_data,
    resource_config_error,
    resource_envelope,
)


def _resource(**capabilities: object) -> Resource:
    now = datetime.now(UTC)
    return Resource(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        created_at=now,
        updated_at=now,
        kind=ResourceKind.LOCAL_LIBVIRT,
        capabilities=capabilities,
        pool="default",
        cost_class="standard",
        status=ResourceStatus.AVAILABLE,
        host_uri="qemu:///system",
    )


def test_resource_capability_data_flattens_known_capabilities() -> None:
    data = resource_capability_data(
        _resource(
            arch="x86_64",
            vcpus=8,
            memory_mb=16384,
            concurrent_allocation_cap=2,
            transports=["ssh", "gdbstub"],
            ignored={"nested": "value"},
        )
    )

    assert data == {
        "kind": "local-libvirt",
        "arch": "x86_64",
        "vcpus": "8",
        "memory_mb": "16384",
        "concurrent_allocation_cap": "2",
        "transports": "ssh,gdbstub",
    }


def test_resource_envelope_uses_resource_status_and_next_actions() -> None:
    response = resource_envelope(_resource(arch="x86_64"), next_actions=["resources.get"])

    assert response.object_id == "11111111-1111-1111-1111-111111111111"
    assert response.status == "available"
    assert response.suggested_next_actions == ["resources.get"]
    assert response.data["kind"] == "local-libvirt"
    assert response.data["arch"] == "x86_64"


def test_resource_config_error_uses_configuration_error_category() -> None:
    response = resource_config_error("bad-resource")

    assert response.status == "error"
    assert response.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert response.object_id == "bad-resource"
