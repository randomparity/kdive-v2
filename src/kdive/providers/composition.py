"""Active provider composition boundary.

This module owns the deployment opt-in table and aggregates provider-owned composition
factories into a ``ProviderResolver`` plus reconciler support ports. Provider-specific
runtime assembly lives next to each provider.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import FAULT_INJECT
from kdive.domain.models import ResourceKind
from kdive.providers.discovery_registration import ProviderDiscoveryRegistration
from kdive.providers.fault_inject import composition as fault_inject_composition
from kdive.providers.fault_inject.faulting.engine import FaultEngine
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.local_libvirt import composition as local_composition
from kdive.providers.reaping import (
    DumpVolumeReaper,
    InfraReaper,
    NullDumpVolumeReaper,
    NullReaper,
    OwnedDomain,
)
from kdive.providers.remote_libvirt import composition as remote_composition
from kdive.providers.remote_libvirt.config import is_remote_libvirt_configured
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import DiscoveryRegistrar, ProviderRuntime
from kdive.providers.transport_reset import NullResetter, TransportResetter
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.resources.discovery import ensure_discovered_resource_registered

if TYPE_CHECKING:
    from kdive.reconciler.console_assembly import ConsoleHosting


def _discovery_registrar(registration: ProviderDiscoveryRegistration) -> DiscoveryRegistrar:
    async def register(pool: AsyncConnectionPool) -> None:
        # Known remote limitation: ensure_discovered_resource_registered calls
        # discovery.list_resources() synchronously inside its async transaction, and
        # remote TLS connect has no pre-connect timeout. Async offload is deferred.
        target = registration.target_factory()
        await ensure_discovered_resource_registered(
            pool,
            target.discovery,
            kind=registration.kind,
            resource_id=target.resource_id,
            pool_name=registration.pool_name,
            cost_class=registration.cost_class,
        )

    return register


def _with_discovery_registration(
    runtime: ProviderRuntime, registration: ProviderDiscoveryRegistration
) -> ProviderRuntime:
    return replace(runtime, discovery_registrar=_discovery_registrar(registration))


def build_local_runtime(*, secret_registry: SecretRegistry) -> ProviderRuntime:
    runtime = local_composition.build_runtime(secret_registry=secret_registry)
    return _with_discovery_registration(runtime, local_composition.discovery_registration())


def build_fault_inject_runtime(
    *, inventory: FaultInjectInventory | None = None, engine: FaultEngine | None = None
) -> ProviderRuntime:
    runtime = fault_inject_composition.build_runtime(inventory=inventory, engine=engine)
    return _with_discovery_registration(runtime, fault_inject_composition.discovery_registration())


def build_remote_runtime(*, secret_registry: SecretRegistry) -> ProviderRuntime:
    runtime = remote_composition.build_runtime(secret_registry=secret_registry)
    return _with_discovery_registration(
        runtime, remote_composition.discovery_registration(secret_registry=secret_registry)
    )


async def ensure_local_host_registered(pool: AsyncConnectionPool) -> None:
    await _discovery_registrar(local_composition.discovery_registration())(pool)


async def ensure_fault_inject_resource_registered(pool: AsyncConnectionPool) -> None:
    await _discovery_registrar(fault_inject_composition.discovery_registration())(pool)


def _fault_inject_enabled(enable_fault_inject: bool | None) -> bool:
    """Resolve the opt-in gate: an explicit flag wins, else read the env (default off)."""
    if enable_fault_inject is not None:
        return enable_fault_inject
    return (config.get(FAULT_INJECT) or "").strip().lower() in {"1", "true", "yes"}


def _remote_libvirt_enabled(enable_remote_libvirt: bool | None) -> bool:
    """Resolve the opt-in gate: an explicit flag wins, else operator config presence."""
    if enable_remote_libvirt is not None:
        return enable_remote_libvirt
    return is_remote_libvirt_configured()


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
        fault_inject_inventory: FaultInjectInventory | None = None,
        secret_registry: SecretRegistry | None = None,
    ) -> None:
        self._fault_inject_inventory = fault_inject_inventory or FaultInjectInventory()
        self._secret_registry = secret_registry or SecretRegistry()

    @property
    def secret_registry(self) -> SecretRegistry:
        """Return the redaction registry shared by provider-owned ports."""
        return self._secret_registry

    def build_provider_resolver(
        self,
        *,
        enable_fault_inject: bool | None = None,
        enable_remote_libvirt: bool | None = None,
    ) -> ProviderResolver:
        """Assemble the per-deployment ``ResourceKind -> ProviderRuntime`` registry."""
        runtimes = {
            ResourceKind.LOCAL_LIBVIRT: build_local_runtime(secret_registry=self._secret_registry)
        }
        if _fault_inject_enabled(enable_fault_inject):
            runtimes[ResourceKind.FAULT_INJECT] = build_fault_inject_runtime(
                inventory=self._fault_inject_inventory
            )
        if _remote_libvirt_enabled(enable_remote_libvirt):
            runtimes[ResourceKind.REMOTE_LIBVIRT] = build_remote_runtime(
                secret_registry=self._secret_registry
            )
        return ProviderResolver(runtimes)

    def build_reconciler_reaper(self, *, enable_fault_inject: bool | None = None) -> InfraReaper:
        """Assemble the provider-aware leaked-infra reaper for reconciliation."""
        reapers: list[InfraReaper] = []
        if _fault_inject_enabled(enable_fault_inject):
            reapers.append(fault_inject_composition.build_reaper(self._fault_inject_inventory))
        if not reapers:
            return NullReaper()
        if len(reapers) == 1:
            return reapers[0]
        return _CompositeReaper(tuple(reapers))

    def build_reconciler_transport_resetter(
        self, *, enable_remote_libvirt: bool | None = None
    ) -> TransportResetter:
        """Assemble the reconciler's dead-session transport resetter (ADR-0086)."""
        if _remote_libvirt_enabled(enable_remote_libvirt):
            return remote_composition.build_transport_resetter(
                secret_registry=self._secret_registry
            )
        return NullResetter()

    def build_reconciler_dump_volume_reaper(
        self, *, enable_remote_libvirt: bool | None = None
    ) -> DumpVolumeReaper:
        """Assemble the reconciler's host_dump orphaned-volume reaper (ADR-0094)."""
        if _remote_libvirt_enabled(enable_remote_libvirt):
            return remote_composition.build_dump_volume_reaper(
                secret_registry=self._secret_registry
            )
        return NullDumpVolumeReaper()

    async def build_reconciler_console_hosting(
        self, *, enable_remote_libvirt: bool | None = None
    ) -> ConsoleHosting | None:
        """Assemble provider-owned console hosting for the reconciler."""
        if _remote_libvirt_enabled(enable_remote_libvirt):
            return await remote_composition.build_console_hosting(
                secret_registry=self._secret_registry
            )
        return None


def build_provider_resolver(
    *,
    enable_fault_inject: bool | None = None,
    enable_remote_libvirt: bool | None = None,
    secret_registry: SecretRegistry | None = None,
) -> ProviderResolver:
    """Assemble the per-deployment ``ResourceKind -> ProviderRuntime`` registry."""
    return ProviderComposition(secret_registry=secret_registry).build_provider_resolver(
        enable_fault_inject=enable_fault_inject,
        enable_remote_libvirt=enable_remote_libvirt,
    )


__all__ = [
    "build_fault_inject_runtime",
    "build_local_runtime",
    "build_provider_resolver",
    "build_remote_runtime",
    "ensure_fault_inject_resource_registered",
    "ensure_local_host_registered",
    "ProviderComposition",
]
