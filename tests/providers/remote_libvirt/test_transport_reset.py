"""RemoteLibvirtTransportResetter tests — injected TLS opener + fake conn, no live host."""

from __future__ import annotations

import asyncio
from pathlib import Path

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.transport_reset import RemoteLibvirtTransportResetter
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend
from tests.providers.remote_libvirt.fakes import FakeControlConn, FakeDomain

_GDB_ADDR = "10.0.0.5"
_DOMAIN = "kdive-sys"


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "a"),
        concurrent_allocation_cap=1,
        gdb_addr=_GDB_ADDR,
    )


def _resetter(domain: FakeDomain | None, tmp_path: Path) -> RemoteLibvirtTransportResetter:
    conn = FakeControlConn({_DOMAIN: domain} if domain is not None else {})
    return RemoteLibvirtTransportResetter(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda uri: conn,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
    )


def test_matching_gdbstub_handle_rearms_with_stop_then_start(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub",
            transport_handle=f"gdbstub://{_GDB_ADDR}:1234",
            domain_name=_DOMAIN,
        )

    asyncio.run(scenario())
    assert domain.calls == ["monitor:gdbserver none", "monitor:gdbserver tcp::1234"]


def test_non_gdbstub_transport_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="drgn-live", transport_handle=_DOMAIN, domain_name=_DOMAIN
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_handle_host_not_gdb_addr_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub",
            transport_handle="gdbstub://127.0.0.1:1234",  # a local loopback session, not ours
            domain_name=_DOMAIN,
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_missing_domain_name_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub", transport_handle=f"gdbstub://{_GDB_ADDR}:1234", domain_name=None
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_none_handle_is_a_noop(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN)

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub", transport_handle=None, domain_name=_DOMAIN
        )

    asyncio.run(scenario())
    assert domain.calls == []


def test_monitor_error_maps_to_transport_failure(tmp_path: Path) -> None:
    domain = FakeDomain(_DOMAIN, raise_on={"qemuMonitorCommand": libvirt.VIR_ERR_OPERATION_FAILED})

    async def scenario() -> None:
        await _resetter(domain, tmp_path).reset(
            transport="gdbstub",
            transport_handle=f"gdbstub://{_GDB_ADDR}:1234",
            domain_name=_DOMAIN,
        )

    with pytest.raises(CategorizedError) as exc:
        asyncio.run(scenario())
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE
