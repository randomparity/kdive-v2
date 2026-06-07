"""Active provider composition boundary.

This module is the only place the MCP and worker assembly path constructs local-libvirt
providers. It exposes typed runtime ports for the single local-libvirt provider shipped today;
the capability registry remains a separate provider-selection primitive for the future
multi-provider path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from psycopg_pool import AsyncConnectionPool

from kdive.providers.local_libvirt.build import LocalLibvirtBuild
from kdive.providers.local_libvirt.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.control import LocalLibvirtControl
from kdive.providers.local_libvirt.debug_gdbmi import (
    GdbMiEngine as LocalGdbMiEngine,
)
from kdive.providers.local_libvirt.debug_gdbmi import (
    default_attach_seam,
)
from kdive.providers.local_libvirt.discovery import ensure_local_host_registered
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

type DiscoveryRegistrar = Callable[[AsyncConnectionPool], Awaitable[None]]


class ProviderRuntime:
    """Typed provider ports for the default runtime."""

    def __init__(
        self,
        *,
        provisioner: Provisioner,
        builder: Builder,
        installer: Installer,
        booter: Booter,
        connector: Connector,
        controller: Controller,
        retriever: Retriever,
        crash_postmortem: CrashPostmortem,
        vmcore_introspector: VmcoreIntrospector,
        live_introspector: LiveIntrospector,
        discovery_registrar: DiscoveryRegistrar | None = None,
        attach_seam: AttachSeam = default_attach_seam,
        debug_engine: GdbMiEngine | None = None,
    ) -> None:
        self._provisioner = provisioner
        self._builder = builder
        self._installer = installer
        self._booter = booter
        self._connector = connector
        self._controller = controller
        self._retriever = retriever
        self._crash_postmortem = crash_postmortem
        self._vmcore_introspector = vmcore_introspector
        self._live_introspector = live_introspector
        self._discovery_registrar = discovery_registrar
        self._attach_seam = attach_seam
        self._debug_engine = debug_engine if debug_engine is not None else LocalGdbMiEngine()

    def provisioner(self) -> Provisioner:
        return self._provisioner

    def builder(self) -> Builder:
        return self._builder

    def install_boot(self) -> tuple[Installer, Booter]:
        return self._installer, self._booter

    def connector(self) -> Connector:
        return self._connector

    def controller(self) -> Controller:
        return self._controller

    def retriever(self) -> Retriever:
        return self._retriever

    def crash_postmortem(self) -> CrashPostmortem:
        return self._crash_postmortem

    def vmcore_introspector(self) -> VmcoreIntrospector:
        return self._vmcore_introspector

    def live_introspector(self) -> LiveIntrospector:
        return self._live_introspector

    def attach_seam(self) -> AttachSeam:
        return self._attach_seam

    def debug_engine(self) -> GdbMiEngine:
        return self._debug_engine

    async def register_discovery(self, pool: AsyncConnectionPool) -> None:
        """Run provider first-start discovery registration, if this runtime has one."""
        if self._discovery_registrar is not None:
            await self._discovery_registrar(pool)


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
        discovery_registrar=ensure_local_host_registered,
    )


__all__ = [
    "ProviderRuntime",
    "build_default_provider_runtime",
]
