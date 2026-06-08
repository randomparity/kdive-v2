"""Tests for the local-libvirt Discovery plane (ADR-0023)."""

from __future__ import annotations

import libvirt
import pytest

from kdive.domain.errors import CategorizedError
from kdive.domain.models import ResourceKind
from kdive.domain.pcie import PCIE_DEVICES_KEY
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import ResourceStatus
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from tests.providers.local_libvirt.fakes import (
    FakeDomain,
    FakeLibvirtConn,
    FakeNodeDevice,
    pci_nodedev_xml,
)


def _discovery(conn: FakeLibvirtConn, *, cap: int = 2) -> LocalLibvirtDiscovery:
    return LocalLibvirtDiscovery(
        host_uri="qemu:///system", connect=lambda: conn, concurrent_allocation_cap=cap
    )


def test_list_resources_advertises_host_capabilities() -> None:
    record = _discovery(FakeLibvirtConn(), cap=3).list_resources()[0]
    assert record["resource_id"] == "qemu:///system"
    assert record["kind"] is ResourceKind.LOCAL_LIBVIRT
    assert record["status"] is ResourceStatus.AVAILABLE
    caps = record["capabilities"]
    assert caps["arch"] == "x86_64"
    assert caps["vcpus"] == 8
    assert caps["memory_mb"] == 16384
    assert caps["transports"] == ["gdbstub"]
    assert caps[CONCURRENT_ALLOCATION_CAP_KEY] == 3


def test_list_resources_arch_unknown_when_absent() -> None:
    conn = FakeLibvirtConn(caps_xml="<capabilities><host></host></capabilities>")
    record = _discovery(conn).list_resources()[0]
    assert record["capabilities"]["arch"] == "unknown"


def test_list_resources_empty_pcie_devices_when_no_nodedev() -> None:
    caps = _discovery(FakeLibvirtConn()).list_resources()[0]["capabilities"]
    assert caps[PCIE_DEVICES_KEY] == []


def test_list_resources_populates_pcie_descriptors() -> None:
    conn = FakeLibvirtConn(
        node_devices=[
            FakeNodeDevice("pci_0000_3b_00_0", pci_nodedev_xml()),
            FakeNodeDevice(
                "pci_0000_00_1f_0",
                pci_nodedev_xml(
                    name="pci_0000_00_1f_0",
                    cls="0x060100",
                    bus=0,
                    slot=31,  # decimal 31 == hex 1f
                    function=0,
                    vendor_id="0x8086",
                    device_id="0x7a8a",
                    product_label=None,  # self-closing <product/>, no text
                ),
            ),
        ]
    )
    devices = _discovery(conn).list_resources()[0]["capabilities"][PCIE_DEVICES_KEY]
    assert devices[0] == {
        "bdf": "0000:3b:00.0",
        "vendor_id": "8086",
        "device_id": "1572",
        "class_code": "020000",
        "label": "Ethernet Controller X710",
    }
    # Decimal slot 31 → hex 1f in the bdf; empty product text falls back to vendor:device.
    assert devices[1]["bdf"] == "0000:00:1f.0"
    assert devices[1]["device_id"] == "7a8a"
    assert devices[1]["class_code"] == "060100"
    assert devices[1]["label"] == "8086:7a8a"


def test_pcie_descriptor_has_no_free_flag() -> None:
    conn = FakeLibvirtConn(node_devices=[FakeNodeDevice("pci_0000_3b_00_0", pci_nodedev_xml())])
    descriptor = _discovery(conn).list_resources()[0]["capabilities"][PCIE_DEVICES_KEY][0]
    assert "free" not in descriptor


def test_malformed_nodedev_is_skipped_not_fatal() -> None:
    conn = FakeLibvirtConn(
        node_devices=[
            FakeNodeDevice("broken", "<device><name>broken</name></device>"),  # no pci capability
            FakeNodeDevice("pci_0000_3b_00_0", pci_nodedev_xml()),
        ]
    )
    devices = _discovery(conn).list_resources()[0]["capabilities"][PCIE_DEVICES_KEY]
    assert [d["bdf"] for d in devices] == ["0000:3b:00.0"]


def test_list_owned_returns_only_tagged_domains() -> None:
    conn = FakeLibvirtConn(
        domains=[
            FakeDomain("kdive-1", system_id="11111111-1111-1111-1111-111111111111"),
            FakeDomain("other-vm", system_id=None),  # untagged → skipped
        ]
    )
    owned = _discovery(conn).list_owned()
    assert owned == [
        {"system_id": "11111111-1111-1111-1111-111111111111", "domain_name": "kdive-1"}
    ]


def test_list_owned_reraises_non_metadata_libvirt_error() -> None:
    conn = FakeLibvirtConn(
        domains=[FakeDomain("vm", system_id=None, raise_code=libvirt.VIR_ERR_INTERNAL_ERROR)]
    )
    with pytest.raises(CategorizedError):
        _discovery(conn).list_owned()


def test_from_env_reads_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    monkeypatch.setenv("KDIVE_LIBVIRT_ALLOCATION_CAP", "4")
    disc = LocalLibvirtDiscovery.from_env()
    assert disc.concurrent_allocation_cap == 4
    assert disc.host_uri == "qemu:///system"


def test_from_env_defaults_cap_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_LIBVIRT_ALLOCATION_CAP", raising=False)
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    assert LocalLibvirtDiscovery.from_env().concurrent_allocation_cap == 1


def test_from_env_non_int_cap_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    monkeypatch.setenv("KDIVE_LIBVIRT_ALLOCATION_CAP", "lots")
    with pytest.raises(CategorizedError):
        LocalLibvirtDiscovery.from_env()
