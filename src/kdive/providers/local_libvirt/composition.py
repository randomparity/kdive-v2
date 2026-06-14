"""Local-libvirt provider runtime composition."""

from __future__ import annotations

from pathlib import Path

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import ResourceKind
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
from kdive.providers.discovery_registration import (
    DiscoveryRegistrationTarget,
    ProviderDiscoveryRegistration,
)
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
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.local_libvirt.reaping import LibvirtInfraReaper
from kdive.providers.local_libvirt.retrieve import LocalLibvirtRetrieve
from kdive.providers.local_libvirt.rootfs_build import LocalLibvirtRootfsBuildPlane
from kdive.providers.reaping import InfraReaper
from kdive.providers.runtime import DebugCapabilities, ProviderRuntime
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

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


def discovery_registration() -> ProviderDiscoveryRegistration:
    return ProviderDiscoveryRegistration(
        target_factory=_discovery_target,
        kind=ResourceKind.LOCAL_LIBVIRT,
        pool_name=_POOL,
        cost_class=_COST_CLASS,
    )


def _discovery_target() -> DiscoveryRegistrationTarget:
    discovery = LocalLibvirtDiscovery.from_env()
    return DiscoveryRegistrationTarget(discovery=discovery, resource_id=discovery.host_uri)


def build_reaper() -> InfraReaper:
    """Build the local-libvirt reconciler reaper (ADR-0111); opens no connection here."""
    return LibvirtInfraReaper.from_env()


def build_rootfs_build_plane(*, workspace: Path | None = None) -> LocalLibvirtRootfsBuildPlane:
    """Build the local-libvirt rootfs build plane; runs no tool and opens no connection.

    ``workspace`` overrides the default build/publish location (the ``build-fs --workspace``
    operator flag), so an image can be built under a user-writable path.
    """
    return LocalLibvirtRootfsBuildPlane.from_env(workspace=workspace)


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
        profile_policy=LocalLibvirtProfilePolicy(),
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
        debug=DebugCapabilities(
            attach_seam=default_attach_seam,
            engine=GdbMiEngine(redactor_factory=lambda: Redactor(registry=secret_registry)),
        ),
        component_sources=_component_sources(),
        build_config_validator=builder.validate_config_ref,
        rootfs_validator=provisioner.validate_rootfs_ref,
        rootfs_build_plane=LocalLibvirtRootfsBuildPlane.from_env(),
    )
