"""Fault-inject Provisioning plane."""

from __future__ import annotations

from uuid import UUID

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.fault_inject.inventory import FaultInjectInventory


def domain_name(system_id: UUID) -> str:
    return f"fault-inject-{system_id}"


class FaultInjectProvisioning:
    """Provisioner port: mint a synthetic domain and track it in the mock inventory."""

    def __init__(self, inventory: FaultInjectInventory) -> None:
        self._inventory = inventory

    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        del profile
        domain = domain_name(system_id)
        self._inventory.record(system_id, domain)
        return domain

    def teardown(self, domain_name: str) -> None:
        self._inventory.forget(domain_name)

    def reprovision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        self._inventory.forget(domain_name(system_id))
        return self.provision(system_id, profile)
