"""The mock infra-inventory seam (ADR-0072).

The fault-inject provider owns no real infrastructure, so it tracks the synthetic
domains it provisions in an in-process :class:`FaultInjectInventory`. The
:class:`FaultInjectReaper` exposes that inventory in the reconciler's ``InfraReaper``
shape (async ``list_owned``/``destroy``), so the leaked-domain repair pass has synthetic
infra to find and reap (the reconciler validation lands in a later issue).

The inventory is shared by reference: the provisioner records a domain, teardown/control
forgets it, and the reaper reads the same map — so a domain that outlives its System row
is a *leaked* domain the reaper reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OwnedDomain:
    """A synthetic domain the mock owns, in the reconciler ``OwnedDomain`` shape."""

    name: str
    system_id: UUID | None


class FaultInjectInventory:
    """In-process registry of the synthetic domains the mock provider owns."""

    def __init__(self) -> None:
        self._domains: dict[str, UUID] = {}

    def record(self, system_id: UUID, domain_name: str) -> None:
        """Register a synthetic domain as owned by ``system_id`` (idempotent per name)."""
        self._domains[domain_name] = system_id

    def forget(self, domain_name: str) -> None:
        """Drop a domain from the inventory; a missing name is a no-op (idempotent)."""
        self._domains.pop(domain_name, None)

    def owned_domains(self) -> list[OwnedDomain]:
        """Return every owned domain, newest registrations last."""
        return [OwnedDomain(name=name, system_id=sid) for name, sid in self._domains.items()]


class FaultInjectReaper:
    """The reconciler ``InfraReaper`` port backed by a :class:`FaultInjectInventory`."""

    def __init__(self, inventory: FaultInjectInventory) -> None:
        self._inventory = inventory

    async def list_owned(self) -> list[OwnedDomain]:
        """Return the synthetic domains the mock currently owns."""
        return self._inventory.owned_domains()

    async def destroy(self, name: str) -> None:
        """Reap a synthetic domain; destroying an unknown name is a no-op (idempotent)."""
        self._inventory.forget(name)
