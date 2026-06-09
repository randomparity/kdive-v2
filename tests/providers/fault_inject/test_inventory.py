"""The mock infra-inventory seam: provision records a domain the reaper can find/reap."""

from __future__ import annotations

import asyncio
from uuid import UUID

from kdive.providers.fault_inject.inventory import FaultInjectInventory, FaultInjectReaper

_SYSTEM = UUID("11111111-1111-1111-1111-111111111111")
_OTHER = UUID("22222222-2222-2222-2222-222222222222")


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


def test_flag_orphan_marks_only_the_named_domain_and_keeps_it_owned() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")
    inventory.record(_OTHER, "fault-inject-domain-2")

    inventory.flag_orphan("fault-inject-domain-1")

    # The flagged domain is still owned (orphan-flagged leaves the entry for the reaper).
    assert {d.name for d in inventory.owned_domains()} == {
        "fault-inject-domain-1",
        "fault-inject-domain-2",
    }
    assert inventory.is_orphaned("fault-inject-domain-1") is True
    # The flag is per-domain: a sibling domain is not collaterally flagged.
    assert inventory.is_orphaned("fault-inject-domain-2") is False


def test_forget_clears_the_orphan_flag_so_a_reused_name_is_not_stale() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")
    inventory.flag_orphan("fault-inject-domain-1")

    inventory.forget("fault-inject-domain-1")
    # A later System reusing the same domain name must not inherit a stale orphan flag.
    inventory.record(_OTHER, "fault-inject-domain-1")

    assert inventory.is_orphaned("fault-inject-domain-1") is False


def test_flag_orphan_is_idempotent() -> None:
    inventory = FaultInjectInventory()
    inventory.record(_SYSTEM, "fault-inject-domain-1")

    inventory.flag_orphan("fault-inject-domain-1")
    inventory.flag_orphan("fault-inject-domain-1")

    assert inventory.is_orphaned("fault-inject-domain-1") is True


def test_is_orphaned_is_false_for_an_unknown_domain() -> None:
    inventory = FaultInjectInventory()

    # An unrecorded name is simply not orphaned — no KeyError.
    assert inventory.is_orphaned("never-provisioned") is False


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
