"""The mock infra-inventory seam: provision records a domain the reaper can find/reap."""

from __future__ import annotations

import asyncio
from uuid import UUID

from kdive.providers.fault_inject.inventory import FaultInjectInventory, FaultInjectReaper

_SYSTEM = UUID("11111111-1111-1111-1111-111111111111")


def test_recorded_domain_is_listed_by_the_reaper() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")

    owned = asyncio.run(FaultInjectReaper(inventory).list_owned())

    assert [(d.name, d.system_id) for d in owned] == [("fault-inject-domain-1", _SYSTEM)]


def test_destroy_removes_the_domain_from_the_inventory() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")
    reaper = FaultInjectReaper(inventory)

    asyncio.run(reaper.destroy("fault-inject-domain-1"))

    assert asyncio.run(reaper.list_owned()) == []


def test_destroy_is_idempotent_for_an_unknown_domain() -> None:
    reaper = FaultInjectReaper(FaultInjectInventory())

    # A reaper destroy that races a concurrent teardown must not raise (loop.py contract).
    asyncio.run(reaper.destroy("never-provisioned"))

    assert asyncio.run(reaper.list_owned()) == []


def test_forget_drops_a_domain_so_a_torn_down_system_is_not_reaped_twice() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")

    inventory.forget("fault-inject-domain-1")

    assert inventory.owned_domains() == []


def test_reaper_satisfies_the_reconciler_infra_reaper_protocol() -> None:
    from kdive.reconciler.loop import InfraReaper, OwnedDomain

    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")
    reaper = FaultInjectReaper(inventory)

    # The spec names "the InfraReaper shape" — assert the mock seam the reconciler
    # leaked-domain pass consumes is structurally that port, not a lookalike.
    assert isinstance(reaper, InfraReaper)
    (owned,) = asyncio.run(reaper.list_owned())
    assert isinstance(owned, OwnedDomain)
