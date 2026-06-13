"""Remote-libvirt provider runtime composition."""

from __future__ import annotations

from uuid import UUID

import psycopg

from kdive.db.locks import CONSOLE_HOSTING_LEADER, SessionAdvisoryLock
from kdive.db.pool import create_pool, database_url
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError
from kdive.domain.models import ResourceKind
from kdive.provider_components.references import (
    CONFIG_COMPONENT,
    PATCH_COMPONENT,
    ComponentKind,
    ComponentSourceKind,
)
from kdive.provider_components.validation import ComponentSourceCapabilities
from kdive.providers.debug_common.gdbmi import GdbMiEngine
from kdive.providers.debug_common.hostpolicy import allow_acl_remote
from kdive.providers.discovery_registration import (
    DiscoveryRegistrationTarget,
    ProviderDiscoveryRegistration,
)
from kdive.providers.reaping import BuildVmReaper, DumpVolumeReaper
from kdive.providers.remote_libvirt.build import RemoteLibvirtBuild
from kdive.providers.remote_libvirt.build_vm_reaper import RemoteLibvirtBuildVmReaper
from kdive.providers.remote_libvirt.config import remote_config_from_env
from kdive.providers.remote_libvirt.console.collector import ConsoleCollector
from kdive.providers.remote_libvirt.console.wiring import (
    RemoteConsolePartStore,
    open_remote_console,
)
from kdive.providers.remote_libvirt.debug.gdbmi import remote_attach_seam
from kdive.providers.remote_libvirt.debug.introspect import (
    RemoteLibvirtLiveIntrospect,
    RemoteLibvirtVmcoreIntrospect,
)
from kdive.providers.remote_libvirt.discovery import RemoteLibvirtDiscovery
from kdive.providers.remote_libvirt.dump_volume_reaper import RemoteLibvirtDumpVolumeReaper
from kdive.providers.remote_libvirt.lifecycle.connect import RemoteLibvirtConnect
from kdive.providers.remote_libvirt.lifecycle.control import RemoteLibvirtControl
from kdive.providers.remote_libvirt.lifecycle.install import RemoteLibvirtInstall
from kdive.providers.remote_libvirt.lifecycle.provisioning import RemoteLibvirtProvisioning
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy
from kdive.providers.remote_libvirt.retrieve import RemoteLibvirtRetrieve
from kdive.providers.remote_libvirt.rootfs_build import RemoteLibvirtRootfsBuildPlane
from kdive.providers.remote_libvirt.transport_reset import RemoteLibvirtTransportResetter
from kdive.providers.runtime import DebugCapabilities, ProviderRuntime
from kdive.providers.transport_reset import TransportResetter
from kdive.reconciler.console_assembly import ConsoleHosting
from kdive.reconciler.console_hosting import (
    AsyncioPumpRunner,
    CollectorRegistry,
    ConsoleHostingLoop,
    DbRunningRemoteSystems,
)
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import secret_backend_from_env
from kdive.store.objectstore import object_store_from_env

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


def build_build_vm_reaper(*, secret_registry: SecretRegistry) -> BuildVmReaper:
    return RemoteLibvirtBuildVmReaper.from_env(secret_registry=secret_registry)


async def build_console_hosting(*, secret_registry: SecretRegistry) -> ConsoleHosting | None:
    """Build the single-leader remote console hosting loop, or ``None`` when unconfigured."""
    try:
        conninfo = database_url()
        store = object_store_from_env()
        remote_config = remote_config_from_env()
        secret_backend = secret_backend_from_env(registry=secret_registry)
    except CategorizedError:
        return None

    part_store = RemoteConsolePartStore(store, conninfo)
    leader_conn = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
    lock = SessionAdvisoryLock(leader_conn, CONSOLE_HOSTING_LEADER)
    runner = AsyncioPumpRunner()
    registry = CollectorRegistry(pump_runner=runner)
    host_pool = create_pool(min_size=1)
    await host_pool.open()

    def factory(system_id: object) -> ConsoleCollector:
        if not isinstance(system_id, UUID):
            raise TypeError("console collector factory expected a UUID system_id")
        return ConsoleCollector(
            system_id,
            open_console=lambda sid: open_remote_console(remote_config, secret_backend, sid),
            store=part_store,
            secret_registry=secret_registry,
        )

    loop = ConsoleHostingLoop(
        leader_lock=lock,
        running_systems=DbRunningRemoteSystems(host_pool),
        collector_factory=factory,
        registry=registry,
        pump_runner=runner,
    )
    return ConsoleHosting(loop, registry, leader_conn, host_pool)


def discovery_registration(*, secret_registry: SecretRegistry) -> ProviderDiscoveryRegistration:
    return ProviderDiscoveryRegistration(
        target_factory=lambda: _discovery_target(secret_registry),
        kind=ResourceKind.REMOTE_LIBVIRT,
        pool_name=_POOL,
        cost_class=_COST_CLASS,
    )


def _discovery_target(secret_registry: SecretRegistry) -> DiscoveryRegistrationTarget:
    discovery = RemoteLibvirtDiscovery.from_env(secret_registry=secret_registry)
    return DiscoveryRegistrationTarget(discovery=discovery, resource_id=discovery.host_uri)


def build_runtime(*, secret_registry: SecretRegistry) -> ProviderRuntime:
    """Build remote-libvirt ports; buildable without operator config (ADR-0076)."""
    builder = RemoteLibvirtBuild.from_env(secret_registry=secret_registry)
    installer = RemoteLibvirtInstall.from_env(secret_registry=secret_registry)
    retriever = RemoteLibvirtRetrieve.from_env(secret_registry=secret_registry)
    vmcore_introspector = RemoteLibvirtVmcoreIntrospect.from_env(secret_registry=secret_registry)
    live_introspector = RemoteLibvirtLiveIntrospect.from_env(secret_registry=secret_registry)

    return ProviderRuntime(
        profile_policy=RemoteLibvirtProfilePolicy(),
        provisioner=RemoteLibvirtProvisioning(secret_registry=secret_registry),
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
