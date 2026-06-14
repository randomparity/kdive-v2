"""Unit tests for the remote-libvirt Connect plane (issue #205, ADR-0083).

Drive the gdbstub direct-TCP transport orchestration + the full error contract with injected
fakes (config, domain-XML port reader, RSP probe); no libvirt host, no real socket.
"""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import SystemHandle, TransportHandle, TransportHandleData
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.lifecycle.connect import RemoteLibvirtConnect

_REFS = TlsCertRefs(client_cert_ref="c", client_key_ref="k", ca_cert_ref="a")


def _config(*, gdb_addr: str | None = "10.0.0.5") -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://h/system",
        cert_refs=_REFS,
        concurrent_allocation_cap=1,
        gdb_addr=gdb_addr,
    )


def _connect(*, resolve_port, probe, config: RemoteLibvirtConfig | None = None):
    return RemoteLibvirtConnect(
        config_factory=lambda: config if config is not None else _config(),
        resolve_port=resolve_port,
        probe=probe,
    )


def test_open_gdbstub_returns_handle_for_reachable_stub():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True)
    handle = c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    decoded = TransportHandleData.decode(handle)
    assert (decoded.kind, decoded.host, decoded.port) == ("gdbstub", "10.0.0.5", 47002)


def test_open_gdbstub_unset_gdb_addr_is_configuration_error():
    c = _connect(
        resolve_port=lambda system: 47002,
        probe=lambda host, port: True,
        config=_config(gdb_addr=None),
    )
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_open_gdbstub_unreachable_is_debug_attach_failure():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: False)
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_open_gdbstub_socket_fault_is_transport_failure():
    def boom(host: str, port: int) -> bool:
        raise OSError("connection refused")

    c = _connect(resolve_port=lambda system: 47002, probe=boom)
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "gdbstub")
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE


def test_unknown_kind_is_configuration_error():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True)
    with pytest.raises(CategorizedError) as exc:
        c.open_transport(SystemHandle("kdive-sys"), "ssh")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_open_transport_drgn_live_returns_bare_domain_handle():
    # ADR-0083 §4: in-guest drgn rides the guest agent keyed by domain; the handle IS the
    # bare domain name core derived. No gdb_addr needed, no port resolution, no probe.
    c = _connect(
        resolve_port=lambda system: pytest.fail("must not resolve a port for drgn-live"),
        probe=lambda host, port: pytest.fail("must not probe for drgn-live"),
        config=_config(gdb_addr=None),
    )
    handle = c.open_transport(SystemHandle("kdive-remote-1"), "drgn-live")
    assert str(handle) == "kdive-remote-1"


def test_close_transport_no_ops_on_bare_domain_handle():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True)
    c.close_transport(TransportHandle("kdive-remote-1"))  # bare domain, connectionless: no raise


def test_close_transport_still_validates_schemed_gdbstub_handle():
    c = _connect(resolve_port=lambda system: 47002, probe=lambda host, port: True)
    c.close_transport(TransportHandleData(kind="gdbstub", host="10.0.0.5", port=47002).encode())
    with pytest.raises(CategorizedError):
        c.close_transport(TransportHandle("gdbstub://"))  # schemed but malformed → rejected
