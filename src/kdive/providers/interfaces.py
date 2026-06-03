"""The eight provider-plane Protocols and their handle/value aliases (ADR-0009).

A provider implements only the planes it supports; the registry
(:mod:`kdive.providers.capability`) dispatches by capability match. The cross-plane
handle types are thin aliases in M0 — the concrete classes land with the
local-libvirt provider (#15). ``ProvisioningProfile``/``BuildProfile`` are M0
placeholders (the durable models hold them as inline ``jsonb`` fields, not named
types; a typed model arrives with ADR-0011 / #11). The ninth plane, Allocation, is
the core capacity-checked path — deliberately **not** a Protocol here.
"""

from __future__ import annotations

from typing import Any, NewType, Protocol, TypedDict, runtime_checkable

from kdive.domain.models import Allocation, Run

SystemHandle = NewType("SystemHandle", str)
TransportHandle = NewType("TransportHandle", str)
KernelArtifact = NewType("KernelArtifact", str)
ArtifactRef = NewType("ArtifactRef", str)
BreakpointId = NewType("BreakpointId", str)

type ProvisioningProfile = dict[str, Any]
type BuildProfile = dict[str, Any]
type BreakLocation = dict[str, Any]
type Registers = dict[str, Any]
type PowerAction = str


class ResourceRecord(TypedDict):
    """A discovered resource host (Discovery plane)."""

    resource_id: str
    kind: str
    capabilities: dict[str, Any]
    status: str


class OwnedInfra(TypedDict):
    """Infrastructure a provider owns, for the reconciler (Discovery plane)."""

    system_id: str
    domain_name: str


@runtime_checkable
class DiscoveryPlane(Protocol):
    def list_resources(self) -> list[ResourceRecord]: ...
    def list_owned(self) -> list[OwnedInfra]: ...


@runtime_checkable
class ProvisioningPlane(Protocol):
    def provision(self, alloc: Allocation, profile: ProvisioningProfile) -> SystemHandle: ...
    def teardown(self, system: SystemHandle) -> None: ...


@runtime_checkable
class BuildPlane(Protocol):
    def build(self, run: Run, profile: BuildProfile) -> KernelArtifact: ...


@runtime_checkable
class InstallPlane(Protocol):
    def install(self, system: SystemHandle, kernel: KernelArtifact) -> None: ...


@runtime_checkable
class ConnectPlane(Protocol):
    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle: ...
    def close_transport(self, handle: TransportHandle) -> None: ...


@runtime_checkable
class DebugPlane(Protocol):
    def set_breakpoint(self, h: TransportHandle, loc: BreakLocation) -> BreakpointId: ...
    def read_memory(self, h: TransportHandle, addr: int, length: int) -> bytes:
        """Read guest memory. ``length`` must be ≤ 4096 (enforced by the provider, #15)."""
        ...

    def read_registers(self, h: TransportHandle) -> Registers: ...


@runtime_checkable
class ControlPlane(Protocol):
    def power(self, system: SystemHandle, action: PowerAction) -> None: ...
    def force_crash(self, system: SystemHandle) -> None: ...


@runtime_checkable
class RetrievePlane(Protocol):
    def capture_vmcore(self, system: SystemHandle) -> ArtifactRef: ...
