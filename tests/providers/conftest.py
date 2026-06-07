"""Fakes and helpers for the provider-seam tests (issue #13).

``FakeProvider`` exposes generic plane operation names and can be registered for any
operation. ``PartialFakeProvider`` implements only Build + Discovery.
``UnhonoredProvider`` has no plane methods. ``MutableProvider`` exposes ``build`` as an
instance attribute so a test can delete it after registration to exercise the
at-dispatch honored-method re-check.
"""

from __future__ import annotations

from kdive.domain.models import Allocation, ResourceKind, Run
from kdive.profiles.build import ParsedBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.capability import Capability, CleanupGuarantee, OpContract, Plane
from kdive.providers.interfaces import (
    ArtifactRef,
    BreakLocation,
    BreakpointId,
    KernelArtifact,
    OwnedInfra,
    Registers,
    ResourceRecord,
    SystemHandle,
    TransportHandle,
)
from kdive.providers.ports import PowerAction

LIBVIRT = ResourceKind.LOCAL_LIBVIRT

DEFAULT_CONTRACT = OpContract(
    idempotent=True,
    destructive=False,
    cancelable=False,
    long_running=True,
    cleanup=CleanupGuarantee.BEST_EFFORT,
)


def build_capability(
    *,
    plane: Plane = Plane.BUILD,
    operation: str = "build",
    contract: OpContract = DEFAULT_CONTRACT,
) -> Capability:
    """Construct a Capability for the local-libvirt kind (test helper)."""
    return Capability(plane=plane, operation=operation, resource_kind=LIBVIRT, contract=contract)


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


class UnhonoredProvider:
    """Advertises capabilities it has no method for (no plane methods at all)."""


class MutableProvider:
    """Exposes ``build`` as a deletable instance attribute (at-dispatch re-check)."""

    def __init__(self) -> None:
        def build(run: Run, profile: ParsedBuildProfile) -> KernelArtifact:
            return KernelArtifact("kernel-1")

        self.build = build
