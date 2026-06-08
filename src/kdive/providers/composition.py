"""Active provider composition boundary.

This module is the only place the MCP and worker assembly path constructs local-libvirt
providers. It exposes typed runtime ports for the single local-libvirt provider shipped today;
ADR-0066 removed the superseded capability-registry prototype from production source.
"""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.providers.component_validation import (
    CONFIG_COMPONENT,
    INITRD_COMPONENT,
    KERNEL_COMPONENT,
    PATCH_COMPONENT,
    ROOTFS_COMPONENT,
    VMLINUX_COMPONENT,
    ComponentKind,
    ComponentSourceCapabilities,
    ComponentSourceKind,
)
from kdive.providers.local_libvirt.build import LocalLibvirtBuild
from kdive.providers.local_libvirt.debug.debug_gdbmi import (
    GdbMiEngine as LocalGdbMiEngine,
)
from kdive.providers.local_libvirt.debug.debug_gdbmi import (
    default_attach_seam,
)
from kdive.providers.local_libvirt.debug.introspect_drgn import (
    LocalLibvirtLiveIntrospect,
    LocalLibvirtVmcoreIntrospect,
)
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.lifecycle.control import LocalLibvirtControl
from kdive.providers.local_libvirt.lifecycle.install import LocalLibvirtInstall
from kdive.providers.local_libvirt.lifecycle.provisioning import (
    LocalLibvirtProvisioning,
)
from kdive.providers.local_libvirt.retrieve import LocalLibvirtRetrieve
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProviderRuntime
from kdive.services.resource_discovery import ensure_discovered_resource_registered

_LOCAL_POOL = "local-libvirt"
_LOCAL_COST_CLASS = "local"


def _local_component_sources() -> ComponentSourceCapabilities:
    accepted: dict[ComponentKind, frozenset[ComponentSourceKind]] = {
        ROOTFS_COMPONENT: frozenset({"catalog", "local"}),
        KERNEL_COMPONENT: frozenset({"local"}),
        INITRD_COMPONENT: frozenset({"local"}),
        CONFIG_COMPONENT: frozenset({"local"}),
        PATCH_COMPONENT: frozenset({"local"}),
        VMLINUX_COMPONENT: frozenset({"local"}),
    }
    return ComponentSourceCapabilities(
        provider=ResourceKind.LOCAL_LIBVIRT.value,
        accepted_component_sources=accepted,
    )


def build_local_runtime() -> ProviderRuntime:
    """Build the typed local-libvirt provider ports without opening live provider connections."""
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
        attach_seam=default_attach_seam,
        debug_engine=LocalGdbMiEngine(),
        component_sources=_local_component_sources(),
        build_config_validator=builder.validate_config_ref,
        rootfs_validator=provisioner.validate_rootfs_ref,
    )


def build_provider_resolver(*, enable_fault_inject: bool = False) -> ProviderResolver:
    """Assemble the per-deployment ``ResourceKind -> ProviderRuntime`` registry.

    The default production composition registers only ``local-libvirt``. The
    ``fault-inject`` provider is opt-in (ADR-0071) and its runtime lands in M1.5
    issue 2; enabling the gate before then is a configuration error, never a
    silent no-op.
    """
    runtimes = {ResourceKind.LOCAL_LIBVIRT: build_local_runtime()}
    if enable_fault_inject:
        raise CategorizedError(
            "fault-inject provider is not yet registered (M1.5 issue 2); "
            "do not enable the gate before its runtime exists",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"enable_fault_inject": True},
        )
    return ProviderResolver(runtimes)


async def ensure_local_host_registered(pool: AsyncConnectionPool) -> None:
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
    "build_local_runtime",
    "build_provider_resolver",
    "ensure_local_host_registered",
]
