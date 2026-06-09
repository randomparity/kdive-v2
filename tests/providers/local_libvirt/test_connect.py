"""Connect-plane provider tests — RSP framing + the seam-injected Connector (no live_vm).

The RSP-framing codec and the `Connector` orchestration (loopback check, prober dispatch,
error mapping, handle codec) are covered with fakes; the real socket / libvirt-domain
endpoint paths are `live_vm`-gated seams exercised only under the gate.
"""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle import connect as connect_mod
from kdive.providers.local_libvirt.lifecycle.connect import (
    LocalLibvirtConnect,
    rsp_frame,
    valid_rsp_frame,
)
from kdive.providers.ports import SystemHandle, TransportHandleData

# --- RSP framing codec ---------------------------------------------------------------------


def test_rsp_frame_wraps_with_mod256_checksum() -> None:
    # '?' is 0x3f; checksum of a single 0x3f payload is 0x3f.
    assert rsp_frame("?") == b"$?#3f"


def test_valid_rsp_frame_accepts_complete_checksum_valid_frame() -> None:
    assert valid_rsp_frame(b"$?#3f") is True


def test_valid_rsp_frame_ignores_leading_ack() -> None:
    assert valid_rsp_frame(b"+$?#3f") is True


def test_valid_rsp_frame_rejects_bare_ack() -> None:
    assert valid_rsp_frame(b"+") is False


def test_valid_rsp_frame_rejects_unterminated_frame() -> None:
    assert valid_rsp_frame(b"$hello") is False


def test_valid_rsp_frame_rejects_non_hex_checksum() -> None:
    assert valid_rsp_frame(b"$?#zz") is False


def test_valid_rsp_frame_rejects_checksum_mismatch() -> None:
    assert valid_rsp_frame(b"$?#00") is False


# --- TransportHandleData codec -------------------------------------------------------------


def test_transport_handle_roundtrips() -> None:
    handle = TransportHandleData(kind="gdbstub", host="127.0.0.1", port=1234)
    encoded = handle.encode()
    assert TransportHandleData.decode(encoded) == handle


def test_transport_handle_decode_malformed_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        TransportHandleData.decode("not-a-handle")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_ssh_transport_handle_roundtrips() -> None:
    handle = TransportHandleData(kind="ssh", host="127.0.0.1", port=22)
    encoded = handle.encode()
    assert encoded.startswith("ssh://")
    assert TransportHandleData.decode(encoded) == handle


def test_transport_handle_decode_unknown_scheme_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        TransportHandleData.decode("telnet://127.0.0.1:23")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- Connector orchestration ---------------------------------------------------------------


class _FakeProbe:
    """Records (host, port) calls; returns a canned result or raises a canned error."""

    def __init__(self, *, result: bool = True, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> bool:
        self.calls.append((host, port))
        if self._raises is not None:
            raise self._raises
        return self._result


def _connector(
    probe: _FakeProbe, *, host: str = "127.0.0.1", port: int = 1234
) -> LocalLibvirtConnect:
    return LocalLibvirtConnect(resolve_endpoint=lambda _system: (host, port), probe=probe)


_SYSTEM = SystemHandle("kdive-x")


def test_open_transport_non_gdbstub_kind_is_configuration_error_without_probing() -> None:
    probe = _FakeProbe()
    with pytest.raises(CategorizedError) as exc:
        _connector(probe).open_transport(_SYSTEM, "tcp")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert probe.calls == []  # rejected before any IO


def test_open_transport_non_loopback_host_is_configuration_error_without_probing() -> None:
    probe = _FakeProbe()
    with pytest.raises(CategorizedError) as exc:
        _connector(probe, host="10.0.0.1").open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert probe.calls == []  # F2: no outbound connect to a non-loopback host


def test_open_transport_hostname_host_is_configuration_error_without_probing() -> None:
    probe = _FakeProbe()
    with pytest.raises(CategorizedError) as exc:
        _connector(probe, host="evil.example").open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert probe.calls == []  # a hostname is not a loopback IP literal — reject without DNS


def test_open_transport_unreachable_stub_is_debug_attach_failure() -> None:
    probe = _FakeProbe(result=False)
    with pytest.raises(CategorizedError) as exc:
        _connector(probe).open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert probe.calls == [("127.0.0.1", 1234)]


def test_open_transport_socket_fault_is_transport_failure() -> None:
    probe = _FakeProbe(raises=OSError("connection reset"))
    with pytest.raises(CategorizedError) as exc:
        _connector(probe).open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE


def test_open_transport_reachable_stub_returns_decodable_handle() -> None:
    probe = _FakeProbe(result=True)
    handle = _connector(probe).open_transport(_SYSTEM, "gdbstub")
    decoded = TransportHandleData.decode(str(handle))
    assert decoded == TransportHandleData(kind="gdbstub", host="127.0.0.1", port=1234)


def test_from_env_resolver_raises_missing_dependency() -> None:
    connector = LocalLibvirtConnect.from_env()
    with pytest.raises(CategorizedError) as exc:
        connector.open_transport(_SYSTEM, "gdbstub")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_close_transport_is_noop_and_never_raises() -> None:
    probe = _FakeProbe()
    connector = _connector(probe)
    handle = connector.open_transport(_SYSTEM, "gdbstub")
    connector.close_transport(handle)  # no raise


def test_close_transport_rejects_malformed_handle() -> None:
    probe = _FakeProbe()
    connector = connect_mod.LocalLibvirtConnect(
        resolve_endpoint=lambda _s: ("127.0.0.1", 1), probe=probe
    )
    with pytest.raises(CategorizedError) as exc:
        connector.close_transport(connect_mod.TransportHandle("garbage"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- SSH transport orchestration (ADR-0039) ------------------------------------------------


class _FakeSshConnect:
    """Records (host, port) calls; returns True or raises a canned error."""

    def __init__(self, *, result: bool = True, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> bool:
        self.calls.append((host, port))
        if self._raises is not None:
            raise self._raises
        return self._result


def _ssh_connector(
    ssh_connect: _FakeSshConnect, *, host: str = "127.0.0.1", port: int = 22
) -> LocalLibvirtConnect:
    return LocalLibvirtConnect(
        resolve_endpoint=lambda _system: ("127.0.0.1", 1234),
        probe=_FakeProbe(),
        resolve_ssh_endpoint=lambda _system: (host, port),
        ssh_connect=ssh_connect,
    )


def test_open_ssh_transport_returns_decodable_ssh_handle() -> None:
    ssh = _FakeSshConnect(result=True)
    handle = _ssh_connector(ssh).open_transport(_SYSTEM, "ssh")
    decoded = TransportHandleData.decode(str(handle))
    assert decoded == TransportHandleData(kind="ssh", host="127.0.0.1", port=22)
    assert ssh.calls == [("127.0.0.1", 22)]


def test_open_ssh_transport_non_loopback_host_is_configuration_error_without_io() -> None:
    ssh = _FakeSshConnect()
    with pytest.raises(CategorizedError) as exc:
        _ssh_connector(ssh, host="10.0.0.1").open_transport(_SYSTEM, "ssh")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ssh.calls == []  # F2: no outbound SSH connect to a non-loopback host


def test_open_ssh_transport_hostname_host_is_configuration_error_without_io() -> None:
    ssh = _FakeSshConnect()
    with pytest.raises(CategorizedError) as exc:
        _ssh_connector(ssh, host="guest.example").open_transport(_SYSTEM, "ssh")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ssh.calls == []  # a hostname is not a loopback IP literal — reject without DNS


def test_open_ssh_transport_unreachable_is_debug_attach_failure() -> None:
    ssh = _FakeSshConnect(result=False)
    with pytest.raises(CategorizedError) as exc:
        _ssh_connector(ssh).open_transport(_SYSTEM, "ssh")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE


def test_open_ssh_transport_socket_fault_is_transport_failure() -> None:
    ssh = _FakeSshConnect(raises=OSError("connection reset"))
    with pytest.raises(CategorizedError) as exc:
        _ssh_connector(ssh).open_transport(_SYSTEM, "ssh")
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE


def test_open_unsupported_kind_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        _ssh_connector(_FakeSshConnect()).open_transport(_SYSTEM, "telnet")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_from_env_ssh_resolver_raises_missing_dependency() -> None:
    connector = LocalLibvirtConnect.from_env()
    with pytest.raises(CategorizedError) as exc:
        connector.open_transport(_SYSTEM, "ssh")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_close_ssh_transport_is_noop_and_never_raises() -> None:
    connector = _ssh_connector(_FakeSshConnect())
    handle = connector.open_transport(_SYSTEM, "ssh")
    connector.close_transport(handle)  # no raise
