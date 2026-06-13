"""Unit tests for the ephemeral remote-libvirt build VM lifecycle (ADR-0100).

Drives EphemeralBuildVm.session over the same fake provision-connection the provisioning
tests use (no libvirt host). Asserts the build-domain XML shape, the provision→yield→teardown
order, teardown-on-exception, and overlay creation over the base image.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from kdive.providers.build_host.guest_exec_transport import GuestExecBuildTransport
from kdive.providers.remote_libvirt.lifecycle.build_vm import (
    EphemeralBuildVm,
    build_domain_name,
    build_overlay_volume_name,
    render_build_domain_xml,
)
from kdive.providers.remote_libvirt.lifecycle.xml import recorded_gdb_port
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend
from tests.providers.remote_libvirt.test_provisioning import (
    _BASE_VOLUME,
    FakePool,
    FakeProvisionConn,
    FakeVolume,
    _config,
    _ticker,
)

RUN_ID = UUID("00000000-0000-0000-0000-00000000ca11")
DOMAIN_NAME = build_domain_name(RUN_ID)
OVERLAY = build_overlay_volume_name(RUN_ID)


def _agent_ok(domain: Any, command: str, timeout: int, flags: int) -> str:
    """A guest-agent fake good enough for a no-op transport binding (no exec in these tests)."""
    msg = json.loads(command)
    if msg["execute"] == "guest-exec":
        return json.dumps({"return": {"pid": 1}})
    return json.dumps({"return": {"exited": True, "exitcode": 0}})


def _conn_with_base() -> FakeProvisionConn:
    pool = FakePool({_BASE_VOLUME: FakeVolume(_BASE_VOLUME)})
    return FakeProvisionConn({"default": pool})


def _build_vm(conn: FakeProvisionConn, tmp_path: Any) -> EphemeralBuildVm:
    def _open(_uri: str) -> Any:
        return conn

    return EphemeralBuildVm(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=_open,
        agent_command=_agent_ok,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
        sleep=lambda _s: None,
        monotonic=_ticker(),
    )


# --- build-domain XML ---------------------------------------------------------------


def test_render_build_domain_xml_has_agent_channel_and_no_gdbstub() -> None:
    xml = render_build_domain_xml(
        RUN_ID, pool="default", volume=OVERLAY, network="default", machine="pc"
    )
    assert f"<name>{DOMAIN_NAME}</name>" in xml
    # The agent channel must be present (readiness depends on it).
    assert "org.qemu.guest_agent.0" in xml
    # The build domain must record NO gdbstub port (inert for used_gdb_ports enumeration).
    assert recorded_gdb_port(xml) is None


# --- session lifecycle --------------------------------------------------------------


def test_session_provisions_yields_transport_and_tears_down(tmp_path: Any) -> None:
    conn = _conn_with_base()
    vm = _build_vm(conn, tmp_path)

    with vm.session(_BASE_VOLUME, run_id=RUN_ID) as transport:
        assert isinstance(transport, GuestExecBuildTransport)
        # The domain is defined + started while the session is open.
        assert DOMAIN_NAME in conn.domains
        assert conn.domains[DOMAIN_NAME].active

    # After the session exits, the domain is destroyed + undefined and the overlay deleted.
    assert DOMAIN_NAME not in conn.domains
    assert OVERLAY in conn.pools["default"].deleted


def test_session_creates_overlay_over_base_image(tmp_path: Any) -> None:
    conn = _conn_with_base()
    vm = _build_vm(conn, tmp_path)

    with vm.session(_BASE_VOLUME, run_id=RUN_ID):
        [volume_xml] = conn.pools["default"].created_xml
        assert OVERLAY in volume_xml
        assert f"/pool/{_BASE_VOLUME}" in volume_xml


def test_session_tears_down_even_when_body_raises(tmp_path: Any) -> None:
    conn = _conn_with_base()
    vm = _build_vm(conn, tmp_path)

    with pytest.raises(RuntimeError, match="boom"), vm.session(_BASE_VOLUME, run_id=RUN_ID):
        raise RuntimeError("boom")

    # Teardown still ran: domain gone, overlay reclaimed.
    assert DOMAIN_NAME not in conn.domains
    assert OVERLAY in conn.pools["default"].deleted
