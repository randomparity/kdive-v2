"""Tests for the local-libvirt Discovery plane (ADR-0023)."""

from __future__ import annotations

import libvirt
import pytest

from kdive.domain.errors import CategorizedError
from kdive.domain.models import ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import ResourceStatus
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from tests.providers.local_libvirt.fakes import FakeDomain, FakeLibvirtConn


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
