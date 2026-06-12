"""Remote-libvirt provider runtime composition."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.domain.capture import CaptureMethod
from kdive.domain.models import ResourceKind
from kdive.images.planes.remote_libvirt import RemoteLibvirtRootfsBuildPlane
from kdive.provider_components.references import (
    CONFIG_COMPONENT,
    PATCH_COMPONENT,
    ComponentKind,
    ComponentSourceKind,
)
from kdive.provider_components.validation import ComponentSourceCapabilities
from kdive.providers.debug_common.gdbmi import GdbMiEngine
from kdive.providers.debug_common.hostpolicy import allow_acl_remote
from kdive.providers.reaping import DumpVolumeReaper
from kdive.providers.remote_libvirt.build import RemoteLibvirtBuild
from kdive.providers.remote_libvirt.debug.gdbmi import remote_attach_seam
from kdive.providers.remote_libvirt.debug.introspect import (
    RemoteLiveIntrospect,
    RemoteVmcoreIntrospect,
)
from kdive.providers.remote_libvirt.discovery import RemoteLibvirtDiscovery
from kdive.providers.remote_libvirt.dump_volume_reaper import RemoteLibvirtDumpVolumeReaper
from kdive.providers.remote_libvirt.lifecycle.connect import RemoteLibvirtConnect
from kdive.providers.remote_libvirt.lifecycle.control import RemoteLibvirtControl
from kdive.providers.remote_libvirt.lifecycle.install import RemoteLibvirtInstall
from kdive.providers.remote_libvirt.lifecycle.provisioning import RemoteLibvirtProvision
from kdive.providers.remote_libvirt.retrieve import RemoteLibvirtRetrieve
from kdive.providers.remote_libvirt.transport_reset import RemoteLibvirtTransportResetter
from kdive.providers.runtime import DebugCapabilities, ProviderRuntime
from kdive.providers.transport_reset import TransportResetter
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.resources.discovery import ensure_discovered_resource_registered

_POOL = "remote-libvirt"
# Reuses seeded `local`; a remote seed row would be DDL beyond migration 0020.
_COST_CLASS = "local"


def _component_sources() -> ComponentSourceCapabilities:
    # Remote server build merges a kdump config fragment onto the tree's defconfig and applies
    # an optional local patch on the worker (ADR-0081/0096). No rootfs/kernel/initrd: the remote
    # target is a disk-image base OS, not a component-provisioned guest.
    accepted: dict[ComponentKind, frozenset[ComponentSourceKind]] = {
        CONFIG_COMPONENT: frozenset({"catalog", "local"}),
        PATCH_COMPONENT: frozenset({"local"}),
    }
    return ComponentSourceCapabilities(
        provider=ResourceKind.REMOTE_LIBVIRT.value,
        accepted_component_sources=accepted,
    )


def build_transport_resetter(*, secret_registry: SecretRegistry) -> TransportResetter:
    return RemoteLibvirtTransportResetter.from_env(secret_registry=secret_registry)


def build_dump_volume_reaper(*, secret_registry: SecretRegistry) -> DumpVolumeReaper:
    return RemoteLibvirtDumpVolumeReaper.from_env(secret_registry=secret_registry)


def build_runtime(*, secret_registry: SecretRegistry) -> ProviderRuntime:
    """Build remote-libvirt ports; buildable without operator config (ADR-0076)."""
    builder = RemoteLibvirtBuild.from_env(secret_registry=secret_registry)
    installer = RemoteLibvirtInstall.from_env(secret_registry=secret_registry)
    retriever = RemoteLibvirtRetrieve.from_env(secret_registry=secret_registry)
    vmcore_introspector = RemoteVmcoreIntrospect.from_env(secret_registry=secret_registry)
    live_introspector = RemoteLiveIntrospect.from_env(secret_registry=secret_registry)

    async def register_remote_host(pool: AsyncConnectionPool) -> None:
        # Known limitation: ensure_discovered_resource_registered calls
        # discovery.list_resources() synchronously inside its async transaction, and
        # the remote TLS connect has no pre-connect timeout. Async offload is deferred.
        discovery = RemoteLibvirtDiscovery.from_env(secret_registry=secret_registry)
        await ensure_discovered_resource_registered(
            pool,
            discovery,
            kind=ResourceKind.REMOTE_LIBVIRT,
            resource_id=discovery.host_uri,
            pool_name=_POOL,
            cost_class=_COST_CLASS,
        )

    return ProviderRuntime(
        provisioner=RemoteLibvirtProvision(secret_registry=secret_registry),
        builder=builder,
        installer=installer,
        booter=installer,
        connector=RemoteLibvirtConnect.from_env(),
        controller=RemoteLibvirtControl.from_env(secret_registry=secret_registry),
        retriever=retriever,
        crash_postmortem=retriever,
        vmcore_introspector=vmcore_introspector,
        live_introspector=live_introspector,
        supported_capture_methods=frozenset(
            {
                CaptureMethod.KDUMP,
                CaptureMethod.HOST_DUMP,
                CaptureMethod.GDBSTUB,
                CaptureMethod.CONSOLE,
            }
        ),
        discovery_registrar=register_remote_host,
        debug=DebugCapabilities(
            attach_seam=remote_attach_seam,
            engine=GdbMiEngine(
                redactor_factory=lambda: Redactor(registry=secret_registry),
                host_policy=allow_acl_remote,
            ),
        ),
        component_sources=_component_sources(),
        build_config_validator=builder.validate_config_ref,
        rootfs_validator=lambda _rootfs: None,
        rootfs_build_plane=RemoteLibvirtRootfsBuildPlane.from_env(),
    )
