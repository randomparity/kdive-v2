"""Active provider composition boundary.

This module owns the deployment opt-in table and aggregates provider-owned composition
factories into a ``ProviderResolver`` plus reconciler support ports. Provider-specific
runtime assembly lives next to each provider.
"""

from __future__ import annotations

import kdive.config as config
from kdive.config.core_settings import FAULT_INJECT
from kdive.domain.models import ResourceKind
from kdive.providers.fault_inject import composition as faultinject_composition
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
from kdive.providers.transport_reset import NullResetter, TransportResetter
from kdive.security.secrets.secret_registry import SecretRegistry

build_faultinject_runtime = faultinject_composition.build_runtime
build_local_runtime = local_composition.build_runtime
build_remote_runtime = remote_composition.build_runtime
ensure_faultinject_resource_registered = faultinject_composition.ensure_resource_registered
ensure_local_host_registered = local_composition.ensure_resource_registered


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
        faultinject_inventory: FaultInjectInventory | None = None,
        secret_registry: SecretRegistry | None = None,
    ) -> None:
        self._faultinject_inventory = faultinject_inventory or FaultInjectInventory()
        self._secret_registry = secret_registry or SecretRegistry()

    def build_provider_resolver(
        self,
        *,
        enable_fault_inject: bool | None = None,
        enable_remote_libvirt: bool | None = None,
    ) -> ProviderResolver:
        """Assemble the per-deployment ``ResourceKind -> ProviderRuntime`` registry."""
        runtimes = {
            ResourceKind.LOCAL_LIBVIRT: local_composition.build_runtime(
                secret_registry=self._secret_registry
            )
        }
        if _fault_inject_enabled(enable_fault_inject):
            runtimes[ResourceKind.FAULT_INJECT] = faultinject_composition.build_runtime(
                inventory=self._faultinject_inventory
            )
        if _remote_libvirt_enabled(enable_remote_libvirt):
            runtimes[ResourceKind.REMOTE_LIBVIRT] = remote_composition.build_runtime(
                secret_registry=self._secret_registry
            )
        return ProviderResolver(runtimes)

    def build_reconciler_reaper(self, *, enable_fault_inject: bool | None = None) -> InfraReaper:
        """Assemble the provider-aware leaked-infra reaper for reconciliation."""
        reapers: list[InfraReaper] = []
        if _fault_inject_enabled(enable_fault_inject):
            reapers.append(faultinject_composition.build_reaper(self._faultinject_inventory))
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
    "build_faultinject_runtime",
    "build_local_runtime",
    "build_provider_resolver",
    "build_remote_runtime",
    "ensure_faultinject_resource_registered",
    "ensure_local_host_registered",
    "ProviderComposition",
]
