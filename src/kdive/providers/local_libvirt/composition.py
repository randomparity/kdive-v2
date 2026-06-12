"""Local-libvirt provider runtime composition."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import ResourceKind
from kdive.images.planes.local_libvirt import LocalLibvirtRootfsBuildPlane
from kdive.provider_components.references import (
    CONFIG_COMPONENT,
    INITRD_COMPONENT,
    KERNEL_COMPONENT,
    PATCH_COMPONENT,
    ROOTFS_COMPONENT,
    VMLINUX_COMPONENT,
    ComponentKind,
    ComponentSourceKind,
)
from kdive.provider_components.validation import ComponentSourceCapabilities
from kdive.providers.debug_common.gdbmi import GdbMiEngine
from kdive.providers.local_libvirt.build import LocalLibvirtBuild
from kdive.providers.local_libvirt.debug.gdbmi import default_attach_seam
from kdive.providers.local_libvirt.debug.introspect import (
    LocalLibvirtLiveIntrospect,
    LocalLibvirtVmcoreIntrospect,
)
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.local_libvirt.lifecycle.control import LocalLibvirtControl
from kdive.providers.local_libvirt.lifecycle.install import LocalLibvirtInstall
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.providers.local_libvirt.retrieve import LocalLibvirtRetrieve
from kdive.providers.runtime import DebugCapabilities, ProviderRuntime
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.resources.discovery import ensure_discovered_resource_registered

_POOL = "local-libvirt"
_COST_CLASS = "local"


def _component_sources() -> ComponentSourceCapabilities:
    accepted: dict[ComponentKind, frozenset[ComponentSourceKind]] = {
        ROOTFS_COMPONENT: frozenset({"catalog", "local"}),
        KERNEL_COMPONENT: frozenset({"local"}),
        INITRD_COMPONENT: frozenset({"local"}),
        CONFIG_COMPONENT: frozenset({"catalog", "local"}),
        PATCH_COMPONENT: frozenset({"local"}),
        VMLINUX_COMPONENT: frozenset({"local"}),
    }
    return ComponentSourceCapabilities(
        provider=ResourceKind.LOCAL_LIBVIRT.value,
        accepted_component_sources=accepted,
    )


async def ensure_resource_registered(pool: AsyncConnectionPool) -> None:
    discovery = LocalLibvirtDiscovery.from_env()
    await ensure_discovered_resource_registered(
        pool,
        discovery,
        kind=ResourceKind.LOCAL_LIBVIRT,
        resource_id=discovery.host_uri,
        pool_name=_POOL,
        cost_class=_COST_CLASS,
    )


def build_runtime(*, secret_registry: SecretRegistry) -> ProviderRuntime:
    """Build local-libvirt provider ports without opening live provider connections."""
    provisioner = LocalLibvirtProvisioning.from_env()
    builder = LocalLibvirtBuild.from_env(secret_registry=secret_registry)
    install = LocalLibvirtInstall.from_env()
    connector = LocalLibvirtConnect.from_env()
    controller = LocalLibvirtControl.from_env()
    retrieve = LocalLibvirtRetrieve.from_env(secret_registry=secret_registry)
    vmcore_introspector = LocalLibvirtVmcoreIntrospect.from_env(secret_registry=secret_registry)
    live_introspector = LocalLibvirtLiveIntrospect.from_env(secret_registry=secret_registry)
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
        discovery_registrar=ensure_resource_registered,
        debug=DebugCapabilities(
            attach_seam=default_attach_seam,
            engine=GdbMiEngine(redactor_factory=lambda: Redactor(registry=secret_registry)),
        ),
        component_sources=_component_sources(),
        build_config_validator=builder.validate_config_ref,
        rootfs_validator=provisioner.validate_rootfs_ref,
        rootfs_build_plane=LocalLibvirtRootfsBuildPlane.from_env(),
    )
