"""Fault-inject provider runtime composition."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

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
from kdive.providers.fault_inject.discovery import FaultInjectDiscovery
from kdive.providers.fault_inject.faulting.engine import FaultEngine
from kdive.providers.fault_inject.inventory import FaultInjectInventory, FaultInjectReaper
from kdive.providers.fault_inject.lifecycle.faulted import FaultedInstall, FaultedProvision
from kdive.providers.fault_inject.lifecycle.provider import (
    FaultInjectBuild,
    FaultInjectConnect,
    FaultInjectControl,
    FaultInjectDebugEngine,
    FaultInjectInstall,
    FaultInjectIntrospect,
    FaultInjectProvision,
    FaultInjectRetrieve,
    fault_inject_attach_seam,
)
from kdive.providers.reaping import InfraReaper
from kdive.providers.runtime import DebugCapabilities, ProviderRuntime
from kdive.services.resources.discovery import ensure_discovered_resource_registered
from kdive.store.objectstore import object_store_from_env

_POOL = "fault-inject"
# Synthetic provider cost reuses seeded `local`; unseeded classes fail closed in accounting.
_COST_CLASS = "local"


def _component_sources() -> ComponentSourceCapabilities:
    accepted: dict[ComponentKind, frozenset[ComponentSourceKind]] = {
        ROOTFS_COMPONENT: frozenset({"catalog", "local"}),
        KERNEL_COMPONENT: frozenset({"local"}),
        INITRD_COMPONENT: frozenset({"local"}),
        CONFIG_COMPONENT: frozenset({"local"}),
        PATCH_COMPONENT: frozenset({"local"}),
        VMLINUX_COMPONENT: frozenset({"local"}),
    }
    return ComponentSourceCapabilities(
        provider=ResourceKind.FAULT_INJECT.value,
        accepted_component_sources=accepted,
    )


async def ensure_resource_registered(pool: AsyncConnectionPool) -> None:
    # Insert-if-absent (like local): the happy path's capabilities are inert, so this never
    # updates an existing row. Mutable fault-inject resource config needs an explicit upsert
    # path or a fresh resource row, not a restart-only refresh.
    discovery = FaultInjectDiscovery.from_env()
    await ensure_discovered_resource_registered(
        pool,
        discovery,
        kind=ResourceKind.FAULT_INJECT,
        resource_id=discovery.host_uri,
        pool_name=_POOL,
        cost_class=_COST_CLASS,
    )


def build_reaper(inventory: FaultInjectInventory) -> InfraReaper:
    return FaultInjectReaper(inventory)


def build_runtime(
    *, inventory: FaultInjectInventory | None = None, engine: FaultEngine | None = None
) -> ProviderRuntime:
    """Build fault-inject mock provider ports (ADR-0072 happy path; ADR-0074 faults)."""
    inventory = inventory if inventory is not None else FaultInjectInventory()
    provisioner = FaultInjectProvision(inventory)
    install = FaultInjectInstall()
    retrieve = FaultInjectRetrieve(store_factory=object_store_from_env)
    introspect = FaultInjectIntrospect()
    faulted_install = FaultedInstall(install, engine) if engine is not None else install
    return ProviderRuntime(
        provisioner=FaultedProvision(provisioner, engine) if engine is not None else provisioner,
        builder=FaultInjectBuild(store_factory=object_store_from_env),
        installer=faulted_install,
        booter=faulted_install,
        connector=FaultInjectConnect(),
        controller=FaultInjectControl(),
        retriever=retrieve,
        crash_postmortem=retrieve,
        vmcore_introspector=introspect,
        live_introspector=introspect,
        supported_capture_methods=frozenset(
            {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
        ),
        discovery_registrar=ensure_resource_registered,
        debug=DebugCapabilities(
            attach_seam=fault_inject_attach_seam,
            engine=FaultInjectDebugEngine(),
        ),
        component_sources=_component_sources(),
        rootfs_validator=lambda _rootfs: None,
    )
