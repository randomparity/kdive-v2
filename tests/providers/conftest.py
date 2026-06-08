"""Fakes and helpers for the provider-seam tests.

``FakeProvider`` exposes generic plane operation names. ``PartialFakeProvider`` implements
only Build + Discovery.
"""

from __future__ import annotations

from kdive.domain.discovery import ResourceRecord
from kdive.domain.models import Allocation, PowerAction, ResourceKind, Run
from kdive.profiles.build import ParsedBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.interfaces import (
    ArtifactRef,
    BreakLocation,
    BreakpointId,
    KernelArtifact,
    OwnedInfra,
    Registers,
    SystemHandle,
    TransportHandle,
)

LIBVIRT = ResourceKind.LOCAL_LIBVIRT


class FakeProvider:
    """A provider exposing a method for every plane operation."""

    def list_resources(self) -> list[ResourceRecord]:
        return []

    def list_owned(self) -> list[OwnedInfra]:
        return []

    def provision(self, alloc: Allocation, profile: ProvisioningProfile) -> SystemHandle:
        return SystemHandle("sys-1")

    def teardown(self, system: SystemHandle) -> None:
        return None

    def build(self, run: Run, profile: ParsedBuildProfile) -> KernelArtifact:
        return KernelArtifact("kernel-1")

    def install(self, system: SystemHandle, kernel: KernelArtifact) -> None:
        return None

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        return TransportHandle("transport-1")

    def close_transport(self, handle: TransportHandle) -> None:
        return None

    def set_breakpoint(self, h: TransportHandle, loc: BreakLocation) -> BreakpointId:
        return BreakpointId("bp-1")

    def read_memory(self, h: TransportHandle, addr: int, length: int) -> bytes:
        return b""

    def read_registers(self, h: TransportHandle) -> Registers:
        return {}

    def power(self, system: SystemHandle, action: PowerAction) -> None:
        return None

    def force_crash(self, system: SystemHandle) -> None:
        return None

    def capture_vmcore(self, system: SystemHandle) -> ArtifactRef:
        return ArtifactRef("artifact-1")


class PartialFakeProvider:
    """Implements only the Build and Discovery planes."""

    def list_resources(self) -> list[ResourceRecord]:
        return []

    def list_owned(self) -> list[OwnedInfra]:
        return []

    def build(self, run: Run, profile: ParsedBuildProfile) -> KernelArtifact:
        return KernelArtifact("kernel-1")
