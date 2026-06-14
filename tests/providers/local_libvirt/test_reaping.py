"""Tests for the local-libvirt reconciler ``InfraReaper`` adapter (ADR-0111)."""

from __future__ import annotations

import asyncio
from uuid import UUID

from kdive.providers.local_libvirt.reaping import LibvirtInfraReaper
from kdive.providers.ports import OwnedInfra


class _FakeDiscovery:
    def __init__(self, owned: list[OwnedInfra]) -> None:
        self._owned = owned

    def list_owned(self) -> list[OwnedInfra]:
        return list(self._owned)


class _FakeProvisioning:
    def __init__(self) -> None:
        self.torn_down: list[str] = []

    def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)


def _reaper(owned: list[OwnedInfra]) -> tuple[LibvirtInfraReaper, _FakeProvisioning]:
    provisioning = _FakeProvisioning()
    reaper = LibvirtInfraReaper(discovery=_FakeDiscovery(owned), provisioning=provisioning)
    return reaper, provisioning


def test_list_owned_adapts_valid_uuid_tag() -> None:
    reaper, _ = _reaper(
        [{"system_id": "11111111-1111-1111-1111-111111111111", "domain_name": "kdive-1"}]
    )
    owned = asyncio.run(reaper.list_owned())
    assert len(owned) == 1
    assert owned[0].name == "kdive-1"
    assert owned[0].system_id == UUID("11111111-1111-1111-1111-111111111111")


def test_list_owned_maps_empty_system_id_to_none() -> None:
    reaper, _ = _reaper(
        [{"system_id": "", "domain_name": "kdive-22222222-2222-2222-2222-222222222222"}]
    )
    owned = asyncio.run(reaper.list_owned())
    assert owned[0].name == "kdive-22222222-2222-2222-2222-222222222222"
    assert owned[0].system_id is None  # never UUID("") — that would raise


def test_list_owned_maps_unparseable_system_id_to_none() -> None:
    reaper, _ = _reaper([{"system_id": "not-a-uuid", "domain_name": "kdive-x"}])
    owned = asyncio.run(reaper.list_owned())
    assert owned[0].system_id is None


def test_destroy_routes_to_provisioning_teardown() -> None:
    reaper, provisioning = _reaper([])
    asyncio.run(reaper.destroy("kdive-99999999-9999-9999-9999-999999999999"))
    assert provisioning.torn_down == ["kdive-99999999-9999-9999-9999-999999999999"]
