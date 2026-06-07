"""Tests for shared provider interface value types."""

from __future__ import annotations

import kdive.providers.interfaces as interfaces
from kdive.providers.capability import Plane
from kdive.providers.interfaces import (
    ArtifactRef,
    KernelArtifact,
    ResourceRecord,
    SystemHandle,
)


def test_capability_registry_defines_the_provider_planes() -> None:
    assert len(Plane) == 8


def test_stale_plane_protocols_are_not_public_interfaces() -> None:
    for name in (
        "DiscoveryPlane",
        "ProvisioningPlane",
        "BuildPlane",
        "InstallPlane",
        "ConnectPlane",
        "DebugPlane",
        "ControlPlane",
        "RetrievePlane",
        "AllocationPlane",
        "ProvisioningProfile",
        "BuildProfile",
        "PowerAction",
    ):
        assert not hasattr(interfaces, name)


def test_shared_provider_handles_are_distinct_types() -> None:
    system = SystemHandle("system-1")
    kernel = KernelArtifact("kernel-1")
    artifact = ArtifactRef("artifact-1")

    assert system == "system-1"
    assert kernel == "kernel-1"
    assert artifact == "artifact-1"


def test_discovery_records_keep_resource_shape() -> None:
    record: ResourceRecord = {
        "resource_id": "host-1",
        "kind": "local-libvirt",
        "capabilities": {"arch": "x86_64"},
        "status": "available",
    }

    assert record["resource_id"] == "host-1"
