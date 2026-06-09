"""Remote-libvirt discovery over the injected TLS connection (ADR-0076)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import ResourceStatus
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.discovery import RemoteLibvirtDiscovery
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import FakeConn, RecordingBackend

_REFS = TlsCertRefs(
    client_cert_ref="remote/clientcert.pem",
    client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret
    ca_cert_ref="remote/cacert.pem",
)


def _config(cap: int = 2) -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system", cert_refs=_REFS, concurrent_allocation_cap=cap
    )


def test_list_resources_returns_remote_record(tmp_path: Path) -> None:
    conn = FakeConn()
    discovery = RemoteLibvirtDiscovery(
        config=_config(),
        secret_backend=RecordingBackend(),
        open_connection=lambda _uri: conn,
        pki_base_dir=tmp_path,
    )
    records = discovery.list_resources()
    assert len(records) == 1
    record = records[0]
    assert record["kind"] is ResourceKind.REMOTE_LIBVIRT
    assert record["resource_id"] == "qemu+tls://host.example/system"
    assert record["status"] is ResourceStatus.AVAILABLE
    caps = record["capabilities"]
    assert caps["arch"] == "x86_64"
    assert caps["vcpus"] == 8
    assert caps["memory_mb"] == 16384
    assert caps["transports"] == ["gdbstub"]
    assert caps["connect_uri"] == "qemu+tls://host.example/system"
    assert caps["tls_client_cert_ref"] == "remote/clientcert.pem"
    assert caps["tls_client_key_ref"] == "remote/clientkey.pem"  # pragma: allowlist secret
    assert caps["tls_ca_cert_ref"] == "remote/cacert.pem"
    assert caps[CONCURRENT_ALLOCATION_CAP_KEY] == 2
    assert conn.closed  # the discovery op closes its connection
    assert list(tmp_path.iterdir()) == []  # and deletes its pkipath


def test_malformed_capabilities_xml_yields_unknown_arch(tmp_path: Path) -> None:
    class _BadXmlConn(FakeConn):
        def getCapabilities(self) -> str:  # noqa: N802 - libvirt binding name
            return "<not-xml"

    discovery = RemoteLibvirtDiscovery(
        config=_config(),
        secret_backend=RecordingBackend(),
        open_connection=lambda _uri: _BadXmlConn(),
        pki_base_dir=tmp_path,
    )
    assert discovery.list_resources()[0]["capabilities"]["arch"] == "unknown"


def test_from_env_without_uri_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)
    with pytest.raises(CategorizedError) as excinfo:
        RemoteLibvirtDiscovery.from_env(secret_registry=SecretRegistry())
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_capabilities_advertise_provisioning_knobs(tmp_path: Path) -> None:
    discovery = RemoteLibvirtDiscovery(
        config=RemoteLibvirtConfig(
            uri="qemu+tls://host.example/system",
            cert_refs=_REFS,
            concurrent_allocation_cap=2,
            storage_pool="kdive-pool",
            gdb_addr="10.0.0.5",
            gdb_port_min=48000,
            gdb_port_max=48010,
        ),
        secret_backend=RecordingBackend(),
        open_connection=lambda _uri: FakeConn(),
        pki_base_dir=tmp_path,
    )
    caps = discovery.list_resources()[0]["capabilities"]
    assert caps["storage_pool"] == "kdive-pool"
    assert caps["gdbstub_addr"] == "10.0.0.5"
    assert caps["gdbstub_port_min"] == 48000
    assert caps["gdbstub_port_max"] == 48010


def test_capabilities_omit_gdb_addr_when_unset(tmp_path: Path) -> None:
    discovery = RemoteLibvirtDiscovery(
        config=_config(),
        secret_backend=RecordingBackend(),
        open_connection=lambda _uri: FakeConn(),
        pki_base_dir=tmp_path,
    )
    caps = discovery.list_resources()[0]["capabilities"]
    assert "gdbstub_addr" not in caps
    assert caps["storage_pool"] == "default"
    assert caps["gdbstub_port_min"] == 47000
    assert caps["gdbstub_port_max"] == 47099
