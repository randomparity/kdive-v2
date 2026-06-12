"""Neutral provider runtime contract.

The dataclass in this module is the high-level MCP and worker provider seam. It imports only
provider port protocols and domain value types; concrete provider assembly stays in
``kdive.providers.composition``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import DestructiveJobKind
from kdive.images.planes.base import RootfsBuildPlane
from kdive.profiles.provisioning import ProvisioningProfile, RootfsSource
from kdive.provider_components.references import ComponentRef
from kdive.provider_components.validation import ComponentSourceCapabilities
from kdive.providers.ports import (
    AttachSeam,
    Booter,
    Builder,
    Connector,
    Controller,
    CrashPostmortem,
    GdbMiEngine,
    Installer,
    LiveIntrospector,
    Provisioner,
    Retriever,
    VmcoreIntrospector,
)

type DiscoveryRegistrar = Callable[[AsyncConnectionPool], Awaitable[None]]
type BuildConfigValidator = Callable[[ComponentRef], None]
type RootfsValidator = Callable[[RootfsSource], None]


def _unconfigured_component_sources() -> ComponentSourceCapabilities:
    return ComponentSourceCapabilities(provider="unconfigured", accepted_component_sources={})


@dataclass(frozen=True, slots=True)
class DebugCapabilities:
    """Optional live-debug capability group for providers that support gdb/MI."""

    attach_seam: AttachSeam
    engine: GdbMiEngine


class ProfilePolicy(Protocol):
    """Provider-owned behavior derived from a parsed provisioning profile."""

    def rootfs_source(self, profile: ProvisioningProfile) -> RootfsSource | None:
        """Return the rootfs source used by this provider, if any."""

    def ssh_credential_ref(self, profile: ProvisioningProfile) -> str | None:
        """Return the live-SSH credential reference used by this provider, if any."""

    def drgn_live_requires_credential(self, profile: ProvisioningProfile) -> bool:
        """Return whether drgn-live needs a profile credential."""

    def validate_profile(self, profile: ProvisioningProfile) -> None:
        """Run provider-specific static profile validation."""

    def destructive_opt_in(self, profile: ProvisioningProfile, op: DestructiveJobKind) -> bool:
        """Return whether the profile opts into a destructive operation."""

    def capture_method(self, profile: ProvisioningProfile) -> CaptureMethod:
        """Resolve the crash-capture method enabled by the profile."""


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    """Typed provider ports for the active runtime."""

    profile_policy: ProfilePolicy
    provisioner: Provisioner
    builder: Builder
    installer: Installer
    booter: Booter
    connector: Connector
    controller: Controller
    retriever: Retriever
    crash_postmortem: CrashPostmortem
    vmcore_introspector: VmcoreIntrospector
    live_introspector: LiveIntrospector
    supported_capture_methods: frozenset[CaptureMethod] = field(
        default_factory=lambda: frozenset(CaptureMethod)
    )
    discovery_registrar: DiscoveryRegistrar | None = None
    debug: DebugCapabilities | None = None
    component_sources: ComponentSourceCapabilities = field(
        default_factory=_unconfigured_component_sources
    )
    build_config_validator: BuildConfigValidator | None = None
    rootfs_validator: RootfsValidator | None = None
    rootfs_build_plane: RootfsBuildPlane | None = None

    async def register_discovery(self, pool: AsyncConnectionPool) -> None:
        if self.discovery_registrar is not None:
            await self.discovery_registrar(pool)
