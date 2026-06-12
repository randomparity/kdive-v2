"""The mock infra-inventory seam (ADR-0072).

The fault-inject provider owns no real infrastructure, so it tracks the synthetic domains it
provisions in an in-process :class:`FaultInjectInventory`. The :class:`FaultInjectReaper`
exposes that inventory in the provider reaper shape (async ``list_owned``/``destroy``), so
the leaked-domain repair pass has synthetic infra to find and reap.

The inventory is shared by reference: the provisioner records a domain, teardown/control
forgets it, and the reaper reads the same map — so a domain that outlives its System row
is a *leaked* domain the reaper reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from kdive.providers.reaping import OwnedDomain as ReaperOwnedDomain


@dataclass(frozen=True, slots=True)
class OwnedDomain:
    """A synthetic domain the mock owns, in the provider reaper ``OwnedDomain`` shape.

    A concrete dataclass that structurally satisfies the
    :class:`~kdive.providers.reaping.OwnedDomain` protocol (``name`` + ``system_id``), so a
    :class:`FaultInjectReaper` plugs into leaked-domain repair directly.
    """

    name: str
    system_id: UUID | None


class FaultInjectInventory:
    """In-process registry of the synthetic domains the mock provider owns."""

    def __init__(self) -> None:
        self._domains: dict[str, UUID] = {}
        self._orphaned: set[str] = set()

    def record(self, system_id: UUID, domain_name: str) -> None:
        self._domains[domain_name] = system_id

    def forget(self, domain_name: str) -> None:
        self._domains.pop(domain_name, None)
        self._orphaned.discard(domain_name)

    def flag_orphan(self, domain_name: str) -> None:
        """Mark a domain as intentionally orphaned by a mid-op cancel (idempotent).

        The ``ORPHAN_FLAGGED`` cancel policy (``cancel_policy.py``) leaves the domain in the
        inventory rather than rolling it back; the flag records that the residue was left
        *deliberately* by a cancel, so a reaper/operator can tell it apart from best-effort
        tolerated residue. The entry itself is what the leaked-domain reconciler pass reaps.
        """
        self._orphaned.add(domain_name)

    def is_orphaned(self, domain_name: str) -> bool:
        return domain_name in self._orphaned

    def owned_domains(self) -> list[OwnedDomain]:
        return [OwnedDomain(name=name, system_id=sid) for name, sid in self._domains.items()]


class FaultInjectReaper:
    """The reconciler ``InfraReaper`` port backed by a :class:`FaultInjectInventory`."""

    def __init__(self, inventory: FaultInjectInventory) -> None:
        self._inventory = inventory

    async def list_owned(self) -> list[ReaperOwnedDomain]:
        return list(self._inventory.owned_domains())

    async def destroy(self, name: str) -> None:
        self._inventory.forget(name)
