"""Active provider composition boundary.

This module is the only place the MCP and worker assembly path constructs local-libvirt
providers. It exposes typed runtime ports for the single local-libvirt provider shipped today;
the capability registry remains a separate provider-selection primitive for the future
multi-provider path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import ResourceKind
from kdive.providers.local_libvirt.build import LocalLibvirtBuild
from kdive.providers.local_libvirt.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.control import LocalLibvirtControl
from kdive.providers.local_libvirt.debug_gdbmi import (
    GdbMiEngine as LocalGdbMiEngine,
)
from kdive.providers.local_libvirt.debug_gdbmi import (
    default_attach_seam,
)
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.install import LocalLibvirtInstall
from kdive.providers.local_libvirt.introspect_drgn import (
    LocalLibvirtLiveIntrospect,
    LocalLibvirtVmcoreIntrospect,
)
from kdive.providers.local_libvirt.provisioning import (
    LocalLibvirtProvisioning,
)
from kdive.providers.local_libvirt.retrieve import LocalLibvirtRetrieve
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
from kdive.services.resource_discovery import ensure_discovered_resource_registered

type DiscoveryRegistrar = Callable[[AsyncConnectionPool], Awaitable[None]]

_LOCAL_POOL = "local-libvirt"
_LOCAL_COST_CLASS = "local"


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    """Typed provider ports for the default runtime."""

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
    attach_seam: AttachSeam = default_attach_seam
    debug_engine: GdbMiEngine = field(default_factory=LocalGdbMiEngine)

    def install_boot(self) -> tuple[Installer, Booter]:
        return self.installer, self.booter

    async def register_discovery(self, pool: AsyncConnectionPool) -> None:
        """Run provider first-start discovery registration, if this runtime has one."""
        if self.discovery_registrar is not None:
            await self.discovery_registrar(pool)


def build_default_provider_runtime() -> ProviderRuntime:
    """Build typed default provider ports without opening live provider connections."""
    provisioner = LocalLibvirtProvisioning.from_env()
    builder = LocalLibvirtBuild.from_env()
    install = LocalLibvirtInstall.from_env()
    connector = LocalLibvirtConnect.from_env()
    controller = LocalLibvirtControl.from_env()
    retrieve = LocalLibvirtRetrieve.from_env()
    vmcore_introspector = LocalLibvirtVmcoreIntrospect.from_env()
    live_introspector = LocalLibvirtLiveIntrospect.from_env()
    return ProviderRuntime(
        provisioner=provisioner,
        builder=builder,
        installer=install,
        booter=install,
        connector=connector,
        controller=controller,
        retriever=retrieve,
        crash_postmortem=retrieve,
        vmcore_introspector=vmcore_introspector,
        live_introspector=live_introspector,
        supported_capture_methods=frozenset(
            {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
        ),
        discovery_registrar=ensure_local_host_registered,
    )


async def ensure_local_host_registered(pool: AsyncConnectionPool) -> None:
    """Register the local-libvirt host on first start."""
    discovery = LocalLibvirtDiscovery.from_env()
    await ensure_discovered_resource_registered(
        pool,
        discovery,
        kind=ResourceKind.LOCAL_LIBVIRT,
        resource_id=discovery.host_uri,
        pool_name=_LOCAL_POOL,
        cost_class=_LOCAL_COST_CLASS,
    )


__all__ = [
    "ProviderRuntime",
    "build_default_provider_runtime",
]
