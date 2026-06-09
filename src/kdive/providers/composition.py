"""Active provider composition boundary.

This module is the only place the MCP and worker assembly path constructs concrete provider
ports. The default production resolver registers local-libvirt; the fault-inject provider is
an opt-in runtime behind the same ProviderRuntime/ProviderResolver seam. ADR-0066 removed the
superseded capability-registry prototype from production source.
"""

from __future__ import annotations

import os

from psycopg_pool import AsyncConnectionPool

from kdive.components.references import (
    CONFIG_COMPONENT,
    INITRD_COMPONENT,
    KERNEL_COMPONENT,
    PATCH_COMPONENT,
    ROOTFS_COMPONENT,
    VMLINUX_COMPONENT,
    ComponentKind,
    ComponentSourceKind,
)
from kdive.domain.capture import CaptureMethod
from kdive.domain.models import ResourceKind
from kdive.providers.component_validation import (
    ComponentSourceCapabilities,
)
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
from kdive.providers.reaping import InfraReaper, NullReaper, OwnedDomain
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProviderRuntime
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.resources.discovery import ensure_discovered_resource_registered
from kdive.store.objectstore import object_store_from_env

_LOCAL_POOL = "local-libvirt"
_LOCAL_COST_CLASS = "local"
_FAULTINJECT_POOL = "fault-inject"
# The mock's cost is synthetic; it reuses the seeded `local` coefficient so accounting
# resolves (cost resolution fails closed on an unseeded class) without new seed DDL.
_FAULTINJECT_COST_CLASS = "local"
_FAULTINJECT_ENABLE_ENV = "KDIVE_FAULT_INJECT"


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


def build_local_runtime(*, secret_registry: SecretRegistry) -> ProviderRuntime:
    """Build the typed local-libvirt provider ports without opening live provider connections."""
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
        discovery_registrar=ensure_local_host_registered,
        attach_seam=default_attach_seam,
        debug_engine=LocalGdbMiEngine(redactor_factory=lambda: Redactor(registry=secret_registry)),
        component_sources=_local_component_sources(),
        build_config_validator=builder.validate_config_ref,
        rootfs_validator=provisioner.validate_rootfs_ref,
    )


def _faultinject_component_sources() -> ComponentSourceCapabilities:
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


def build_faultinject_runtime(
    *, inventory: FaultInjectInventory | None = None, engine: FaultEngine | None = None
) -> ProviderRuntime:
    """Build the fault-inject mock provider ports (ADR-0072 happy path; ADR-0074 faults).

    Args:
        inventory: The shared infra-inventory the provisioner records synthetic domains
            into. Pass an inventory the caller also holds to build a matching
            :class:`~kdive.providers.fault_inject.inventory.FaultInjectReaper` over the
            same state (the reconciler leaked-domain seam); omit it for a standalone
            runtime with its own inventory.
        engine: When given, the provision/install/boot ports are wrapped in the ADR-0074
            faulting decorators so a seeded fault perturbs those ops; when ``None`` (the
            default, the happy path) the bare synthetic ports are used unchanged.
    """
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
        discovery_registrar=ensure_faultinject_resource_registered,
        attach_seam=fault_inject_attach_seam,
        debug_engine=FaultInjectDebugEngine(),
        component_sources=_faultinject_component_sources(),
        rootfs_validator=lambda _rootfs: None,
    )


def _fault_inject_enabled(enable_fault_inject: bool | None) -> bool:
    """Resolve the opt-in gate: an explicit flag wins, else read the env (default off)."""
    if enable_fault_inject is not None:
        return enable_fault_inject
    return os.environ.get(_FAULTINJECT_ENABLE_ENV, "").strip().lower() in {"1", "true", "yes"}


class _CompositeReaper:
    """Fan out leaked-domain reconciliation across configured provider reapers."""

    def __init__(self, reapers: tuple[InfraReaper, ...]) -> None:
        self._reapers = reapers

    async def list_owned(self) -> list[OwnedDomain]:
        domains: list[OwnedDomain] = []
        for reaper in self._reapers:
            domains.extend(await reaper.list_owned())
        return domains

    async def destroy(self, name: str) -> None:
        for reaper in self._reapers:
            await reaper.destroy(name)


class ProviderComposition:
    """Own provider assembly state that must be shared across constructed ports."""

    def __init__(
        self,
        *,
        faultinject_inventory: FaultInjectInventory | None = None,
        secret_registry: SecretRegistry | None = None,
    ) -> None:
        self._faultinject_inventory = faultinject_inventory or FaultInjectInventory()
        self._secret_registry = secret_registry or SecretRegistry()

    def build_provider_resolver(
        self, *, enable_fault_inject: bool | None = None
    ) -> ProviderResolver:
        """Assemble the per-deployment ``ResourceKind -> ProviderRuntime`` registry."""
        runtimes = {
            ResourceKind.LOCAL_LIBVIRT: build_local_runtime(secret_registry=self._secret_registry)
        }
        if _fault_inject_enabled(enable_fault_inject):
            runtimes[ResourceKind.FAULT_INJECT] = build_faultinject_runtime(
                inventory=self._faultinject_inventory
            )
        return ProviderResolver(runtimes)

    def build_reconciler_reaper(self, *, enable_fault_inject: bool | None = None) -> InfraReaper:
        """Assemble the provider-aware leaked-infra reaper for reconciliation."""
        reapers: list[InfraReaper] = []
        if _fault_inject_enabled(enable_fault_inject):
            reapers.append(FaultInjectReaper(self._faultinject_inventory))
        if not reapers:
            return NullReaper()
        if len(reapers) == 1:
            return reapers[0]
        return _CompositeReaper(tuple(reapers))


def build_provider_resolver(
    *, enable_fault_inject: bool | None = None, secret_registry: SecretRegistry | None = None
) -> ProviderResolver:
    """Assemble the per-deployment ``ResourceKind -> ProviderRuntime`` registry.

    The default production composition registers only ``local-libvirt``. The
    ``fault-inject`` provider is opt-in (ADR-0071): it is registered only when the gate is
    on — an explicit ``enable_fault_inject`` argument when given, otherwise the
    ``KDIVE_FAULT_INJECT`` env var. A default production deployment has no bookable
    fault-inject Resource.
    """
    return ProviderComposition(secret_registry=secret_registry).build_provider_resolver(
        enable_fault_inject=enable_fault_inject
    )


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


async def ensure_faultinject_resource_registered(pool: AsyncConnectionPool) -> None:
    # Insert-if-absent (like local): the happy path's capabilities are inert, so this never
    # updates an existing row. Mutable fault-inject resource config needs an explicit upsert
    # path or a fresh resource row, not a restart-only refresh.
    discovery = FaultInjectDiscovery.from_env()
    await ensure_discovered_resource_registered(
        pool,
        discovery,
        kind=ResourceKind.FAULT_INJECT,
        resource_id=discovery.host_uri,
        pool_name=_FAULTINJECT_POOL,
        cost_class=_FAULTINJECT_COST_CLASS,
    )


__all__ = [
    "build_faultinject_runtime",
    "build_local_runtime",
    "build_provider_resolver",
    "ensure_faultinject_resource_registered",
    "ensure_local_host_registered",
    "ProviderComposition",
]
