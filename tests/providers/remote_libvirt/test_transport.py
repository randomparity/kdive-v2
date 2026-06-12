"""qemu+tls transport: URI validation, pkipath lifecycle, connection context (ADR-0077)."""

from __future__ import annotations

import stat
from pathlib import Path
from urllib.parse import unquote

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.transport import (
    compose_pkipath_uri,
    materialized_pkipath,
    remote_connection,
)
from kdive.providers.remote_libvirt.uri_validation import validate_remote_uri
from kdive.security.secrets.paths import PathSafetyError
from tests.providers.remote_libvirt.conftest import FakeConn, RecordingBackend

_REFS = TlsCertRefs(
    client_cert_ref="remote/clientcert.pem",
    client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret
    ca_cert_ref="remote/cacert.pem",
)


def _config(uri: str = "qemu+tls://host.example/system") -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(uri=uri, cert_refs=_REFS, concurrent_allocation_cap=1)


@pytest.mark.parametrize(
    "uri",
    [
        "qemu+ssh://host.example/system",
        "qemu:///system",
        "qemu+tcp://host.example/system",
        "qemu+tls://host.example/system?no_verify=1",
        "qemu+tls://host.example/system?no_verify=0",
        "qemu+tls://host.example/system?pkipath=/operator/pki",
        # libvirt matches parameter names case-insensitively and also splits the
        # query on ';' — validation must reject those spellings too (fail closed).
        "qemu+tls://host.example/system?No_Verify=1",
        "qemu+tls://host.example/system?NO_VERIFY=1",
        "qemu+tls://host.example/system?keepalive_interval=5;no_verify=1",
        "qemu+tls://host.example/system?PkiPath=/operator/pki",
        # libvirt percent-unescapes parameter names before matching them.
        "qemu+tls://host.example/system?no%5Fverify=1",
        "qemu+tls://host.example/system?%70kipath=/operator/pki",
    ],
)
def test_validate_rejects_unsafe_uris(uri: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        validate_remote_uri(uri)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_accepts_plain_tls_uri() -> None:
    validate_remote_uri("qemu+tls://host.example/system")


def test_compose_appends_pkipath_preserving_query() -> None:
    uri = compose_pkipath_uri(
        "qemu+tls://host.example/system?keepalive_interval=5", Path("/tmp/pki")
    )
    # `/` stays literal (safe="/"): the URI works whether or not the consumer
    # percent-unescapes query values; mkdtemp paths contain no reserved characters.
    assert uri == "qemu+tls://host.example/system?keepalive_interval=5&pkipath=/tmp/pki"


def test_pkipath_materializes_private_files_and_cleans_up(tmp_path: Path) -> None:
    backend = RecordingBackend()
    with materialized_pkipath(backend, _REFS, base_dir=tmp_path) as pkipath:
        assert stat.S_IMODE(pkipath.stat().st_mode) == 0o700
        for name, ref in [
            ("clientcert.pem", "remote/clientcert.pem"),
            ("clientkey.pem", "remote/clientkey.pem"),  # pragma: allowlist secret
            ("cacert.pem", "remote/cacert.pem"),
        ]:
            file = pkipath / name
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
            assert file.read_text() == f"PEM::{ref}"
    assert backend.resolved == [
        "remote/clientcert.pem",
        "remote/clientkey.pem",
        "remote/cacert.pem",
    ]
    assert list(tmp_path.iterdir()) == []  # deleted on the success path


def test_pkipath_cleans_up_when_body_raises(tmp_path: Path) -> None:
    with (
        pytest.raises(RuntimeError, match="boom"),
        materialized_pkipath(RecordingBackend(), _REFS, base_dir=tmp_path),
    ):
        raise RuntimeError("boom")
    assert list(tmp_path.iterdir()) == []


def test_unresolvable_ref_is_a_configuration_error_and_leaves_no_residue(
    tmp_path: Path,
) -> None:
    # PathSafetyError is a bare ValueError (security/secrets/paths.py); the transport
    # maps it to the platform's typed taxonomy and resolves before any dir is created.
    class _FailingBackend:
        def resolve(self, ref: str) -> str:
            raise PathSafetyError("secret file does not exist")

    with (
        pytest.raises(CategorizedError) as excinfo,
        materialized_pkipath(_FailingBackend(), _REFS, base_dir=tmp_path),
    ):
        pytest.fail("body must not run")
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "remote/clientcert.pem" in str(excinfo.value)  # the ref, never the value
    assert list(tmp_path.iterdir()) == []  # nothing was materialized


def test_materialization_io_failure_is_an_infrastructure_failure(tmp_path: Path) -> None:
    # A full/readonly worker tmp must surface typed, not as a raw OSError.
    readonly = tmp_path / "ro"
    readonly.mkdir()
    readonly.chmod(0o500)
    try:
        with (
            pytest.raises(CategorizedError) as excinfo,
            materialized_pkipath(RecordingBackend(), _REFS, base_dir=readonly),
        ):
            pytest.fail("body must not run")
    finally:
        readonly.chmod(0o700)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_pkipath_cleanup_failure_is_logged_and_never_masks_the_op_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The pinned behavior: a deletion failure is logged at error level and must not
    # replace the op's in-flight typed error (nor break the success path).
    def _refuse(path: object, *args: object, **kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr("kdive.providers.remote_libvirt.transport.shutil.rmtree", _refuse)
    with (
        caplog.at_level("ERROR"),
        pytest.raises(RuntimeError, match="op error"),  # the op error survives
        materialized_pkipath(RecordingBackend(), _REFS, base_dir=tmp_path),
    ):
        raise RuntimeError("op error")
    assert any("pkipath" in record.message for record in caplog.records)


def test_remote_connection_opens_with_pkipath_uri_and_cleans_up(tmp_path: Path) -> None:
    opened: list[str] = []
    conn = FakeConn()

    def open_connection(uri: str) -> FakeConn:
        opened.append(uri)
        # The pkipath must exist (with the key) while the TLS handshake runs.
        pki = Path(unquote(uri.rsplit("pkipath=", 1)[1]))
        assert (pki / "clientkey.pem").is_file()
        return conn

    with remote_connection(
        _config(), RecordingBackend(), open_connection=open_connection, pki_base_dir=tmp_path
    ) as got:
        assert got is conn
    assert conn.closed
    assert list(tmp_path.iterdir()) == []
    assert opened[0].startswith("qemu+tls://host.example/system?pkipath=")


def test_remote_connection_maps_open_failure_to_transport_failure(tmp_path: Path) -> None:
    def open_connection(uri: str) -> FakeConn:
        raise libvirt.libvirtError("handshake failed")

    with (
        pytest.raises(CategorizedError) as excinfo,
        remote_connection(
            _config(),
            RecordingBackend(),
            open_connection=open_connection,
            pki_base_dir=tmp_path,
        ),
    ):
        pytest.fail("body must not run")
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert list(tmp_path.iterdir()) == []  # cleaned up on the failure path too


def test_remote_connection_closes_conn_when_body_raises(tmp_path: Path) -> None:
    conn = FakeConn()
    with (
        pytest.raises(RuntimeError, match="op failed"),
        remote_connection(
            _config(),
            RecordingBackend(),
            open_connection=lambda _uri: conn,
            pki_base_dir=tmp_path,
        ),
    ):
        raise RuntimeError("op failed")
    assert conn.closed
    assert list(tmp_path.iterdir()) == []
