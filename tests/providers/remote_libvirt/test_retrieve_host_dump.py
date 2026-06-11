"""RemoteLibvirtRetrieve host_dump tests — injected libvirt/store/drgn, no host or S3 (ADR-0094).

Self-contained fakes (ADR-0076: the remote provider keeps its own test doubles, no shared
layer): a fake libvirt connection exposing the storage-pool + core-dump slice host_dump drives,
a fake object store, and injected drgn seams (build-id / dmesg). Every assertion is a unit
assertion over the orchestration; nothing touches a real host or MinIO.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import (
    ArtifactStreamRequest,
    ArtifactWriteRequest,
    HeadResult,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
)
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.retrieve import (
    RemoteLibvirtRetrieve,
    host_dump_volume_name,
)
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend

_SID = UUID("00000000-0000-0000-0000-0000000000cc")
_POOL = "default"
_POOL_DIR = "/var/lib/libvirt/images"

_DIR_POOL_XML = f"""
<pool type='dir'>
  <name>{_POOL}</name>
  <target><path>{_POOL_DIR}</path></target>
</pool>
"""

_LVM_POOL_XML = f"""
<pool type='logical'>
  <name>{_POOL}</name>
  <target><path>/dev/{_POOL}</path></target>
</pool>
"""


def _domain_name() -> str:
    return domain_name_for(_SID)


def _config() -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system",
        cert_refs=TlsCertRefs("c", "k", "a"),
        concurrent_allocation_cap=1,
        storage_pool=_POOL,
    )


class FakeVolume:
    """A storage volume the host_dump path resolves + downloads + deletes."""

    def __init__(self, name: str, capacity: int, *, payload: bytes = b"CORE-BYTES") -> None:
        self._name = name
        self._capacity = capacity
        self._payload = payload
        self.deleted = False
        self.download_called = False

    def name(self) -> str:  # noqa: N802 - libvirt binding name
        return self._name

    def info(self) -> list[int]:
        # libvirt virStorageVolInfo: [type, capacity, allocation].
        return [0, self._capacity, self._capacity]

    def download(self, stream: FakeStream, offset: int, length: int, flags: int) -> None:
        self.download_called = True
        stream.feed(self._payload)

    def delete(self, flags: int = 0) -> int:
        self.deleted = True
        return 0


class FakeFailingVolume(FakeVolume):
    """A volume whose download raises, to exercise the cleanup finally."""

    def download(self, stream: object, offset: int, length: int, flags: int) -> None:
        self.download_called = True
        raise RuntimeError("stream dropped mid-download")


class FakeStream:
    """The libvirt stream sink: download() feeds bytes; recvAll drains them to the handler."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        self._buf += data

    def recvAll(  # noqa: N802 - libvirt binding name
        self, handler: Callable[[FakeStream, bytes, object], object], opaque: object
    ) -> None:
        handler(self, bytes(self._buf), opaque)

    def finish(self) -> int:
        return 0

    def abort(self) -> int:
        return 0


class FakePool:
    def __init__(self, *, xml: str, volume: FakeVolume | None) -> None:
        self._xml = xml
        self._volume = volume
        self.refreshed = False
        self.looked_up: list[str] = []

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - binding name
        return self._xml

    def refresh(self, flags: int = 0) -> int:
        self.refreshed = True
        return 0

    def storageVolLookupByName(self, name: str) -> FakeVolume:  # noqa: N802 - binding name
        self.looked_up.append(name)
        if self._volume is None:
            import libvirt

            raise libvirt.libvirtError("no such volume")
        return self._volume


class FakeDomain:
    def __init__(self, name: str) -> None:
        self._name = name
        self.core_dumps: list[tuple[str, int, int]] = []

    def name(self) -> str:  # noqa: N802 - libvirt binding name
        return self._name

    def coreDumpWithFormat(  # noqa: N802 - libvirt binding name
        self, to: str, dumpformat: int, flags: int
    ) -> int:
        self.core_dumps.append((to, dumpformat, flags))
        return 0


class FakeHostDumpConn:
    def __init__(
        self,
        *,
        pool: FakePool | None = None,
        stale_volume: FakeVolume | None = None,
    ) -> None:
        self._domain = FakeDomain(_domain_name())
        self._pool = pool
        self._stale_volume = stale_volume
        self.stream = FakeStream()

    @property
    def domain(self) -> FakeDomain:
        return self._domain

    @property
    def pool(self) -> FakePool | None:
        return self._pool

    def lookupByName(self, name: str) -> FakeDomain:  # noqa: N802 - binding name
        return self._domain

    def storagePoolLookupByName(self, name: str) -> FakePool:  # noqa: N802 - binding name
        assert self._pool is not None
        return self._pool

    def newStream(self, flags: int = 0) -> FakeStream:  # noqa: N802 - binding name
        return self.stream

    def close(self) -> None:
        pass


class FakeStore:
    def __init__(self, *, head: HeadResult | None) -> None:
        self._head = head
        self.stream_requests: list[ArtifactStreamRequest] = []
        self.put_requests: list[ArtifactWriteRequest] = []

    def put_stream(self, request: ArtifactStreamRequest) -> StoredArtifact:
        self.stream_requests.append(request)
        # Read enough to prove the store streams from the path, not an in-RAM buffer.
        with request.path.open("rb") as fh:
            fh.read(1)
        return StoredArtifact(
            request.key(), "etag-raw", request.sensitivity, request.retention_class
        )

    def presign_put(self, request: PresignPutRequest) -> PresignedUpload:
        # The host_dump path uploads from the worker (no presigned PUT); present only to
        # satisfy the store protocol.
        raise AssertionError("host_dump capture must not presign a PUT")

    def head(self, key: str) -> HeadResult | None:
        return self._head

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.put_requests.append(request)
        return StoredArtifact(
            request.key(), "etag-red", request.sensitivity, request.retention_class
        )


def _retrieve(
    conn: FakeHostDumpConn,
    store: FakeStore,
    tmp_path: Path,
    *,
    build_id: str = "deadbeef",
    dmesg: bytes = b"kernel panic\n",
    build_id_error: CategorizedError | None = None,
    max_core_bytes: int = 5 * 1024**3,
) -> RemoteLibvirtRetrieve:
    def _read_build_id(path: Path) -> str:
        if build_id_error is not None:
            raise build_id_error
        assert path.exists()
        return build_id

    def _read_dmesg(path: Path) -> bytes:
        assert path.exists()
        return dmesg

    return RemoteLibvirtRetrieve(
        secret_registry=SecretRegistry(),
        config_factory=_config,
        open_connection=lambda uri: conn,
        store_factory=lambda: store,
        secret_backend_factory=RecordingBackend,
        pki_base_dir=tmp_path,
        core_build_id_from_file=_read_build_id,
        core_dmesg_from_file=_read_dmesg,
        max_core_bytes=max_core_bytes,
    )


def _sha256_b64(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


def _head_ok(payload: bytes = b"CORE-BYTES", *, checksum: str | None = None) -> HeadResult:
    return HeadResult(size_bytes=len(payload), checksum_sha256=checksum, etag="etag-raw")


def test_host_dump_volume_name_is_deterministic_per_system() -> None:
    assert host_dump_volume_name(_SID) == host_dump_volume_name(_SID)
    other = UUID("00000000-0000-0000-0000-0000000000dd")
    assert host_dump_volume_name(_SID) != host_dump_volume_name(other)


def test_host_dump_happy_path_dumps_streams_uploads(tmp_path: Path) -> None:
    vol = FakeVolume(host_dump_volume_name(_SID), capacity=4096)
    pool = FakePool(xml=_DIR_POOL_XML, volume=vol)
    conn = FakeHostDumpConn(pool=pool)
    store = FakeStore(head=_head_ok())

    out = _retrieve(conn, store, tmp_path).capture(_SID, CaptureMethod.HOST_DUMP)

    # AC1: the dump targeted a path inside the pool dir with RAW (ELF) + MEMORY_ONLY.
    import libvirt

    (to, fmt, flags) = conn.domain.core_dumps[0]
    assert to == str(Path(_POOL_DIR) / host_dump_volume_name(_SID))
    # ELF (RAW) memory-only: the only format drgn can open from a QEMU dump (#319, ADR-0094).
    assert fmt == libvirt.VIR_DOMAIN_CORE_DUMP_FORMAT_RAW
    assert flags & libvirt.VIR_DUMP_MEMORY_ONLY
    # AC2: refresh + lookup bridged the path to a volume; the download ran.
    assert pool.refreshed
    assert vol.download_called
    # AC5: the upload streamed from a path, not an in-RAM bytes buffer.
    assert store.stream_requests
    assert store.stream_requests[0].key().endswith("/vmcore-host_dump")
    assert store.stream_requests[0].sensitivity is Sensitivity.SENSITIVE
    # the streamed core's sha256 is bound into the PUT (S3 enforces it), not a vacuous check.
    assert store.stream_requests[0].sha256_b64 == _sha256_b64(b"CORE-BYTES")
    # the redacted dmesg landed too.
    assert out.redacted.key.endswith("/vmcore-host_dump-redacted")
    assert out.vmcore_build_id == "deadbeef"
    # AC7: temp file + host volume both gone.
    assert vol.deleted


def test_host_dump_deletes_a_stale_volume_before_dumping(tmp_path: Path) -> None:
    stale = FakeVolume(host_dump_volume_name(_SID), capacity=4096)
    fresh = FakeVolume(host_dump_volume_name(_SID), capacity=4096)
    # First lookup (pre-dump) returns the stale vol; after dump+refresh the fresh one.
    pool = _StalePool(xml=_DIR_POOL_XML, stale=stale, fresh=fresh)
    conn = FakeHostDumpConn(pool=pool)
    store = FakeStore(head=_head_ok())

    _retrieve(conn, store, tmp_path).capture(_SID, CaptureMethod.HOST_DUMP)

    assert stale.deleted  # AC1: a stale same-named volume is removed before the dump
    assert conn.domain.core_dumps  # the dump still ran afterward


class _StalePool(FakePool):
    """A pool whose first lookup yields a stale volume and whose post-refresh lookup the fresh."""

    def __init__(self, *, xml: str, stale: FakeVolume, fresh: FakeVolume) -> None:
        super().__init__(xml=xml, volume=fresh)
        self._stale = stale
        self._fresh = fresh

    def storageVolLookupByName(self, name: str) -> FakeVolume:  # noqa: N802 - binding name
        self.looked_up.append(name)
        if not self.refreshed:
            return self._stale
        return self._fresh


def test_host_dump_non_dir_pool_is_configuration_error_before_dump(tmp_path: Path) -> None:
    vol = FakeVolume(host_dump_volume_name(_SID), capacity=4096)
    pool = FakePool(xml=_LVM_POOL_XML, volume=vol)
    conn = FakeHostDumpConn(pool=pool)
    store = FakeStore(head=None)

    with pytest.raises(CategorizedError) as exc:
        _retrieve(conn, store, tmp_path).capture(_SID, CaptureMethod.HOST_DUMP)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert not conn.domain.core_dumps  # AC3: no dump into a void


def test_host_dump_over_ceiling_volume_is_configuration_error_before_download(
    tmp_path: Path,
) -> None:
    huge = FakeVolume(host_dump_volume_name(_SID), capacity=6 * 1024**3)
    pool = FakePool(xml=_DIR_POOL_XML, volume=huge)
    conn = FakeHostDumpConn(pool=pool)
    store = FakeStore(head=None)

    with pytest.raises(CategorizedError) as exc:
        _retrieve(conn, store, tmp_path).capture(_SID, CaptureMethod.HOST_DUMP)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert not huge.download_called  # AC4: ceiling enforced before the stream
    assert huge.deleted  # but the over-ceiling dump volume is still cleaned up


def test_host_dump_missing_vmcoreinfo_build_id_is_configuration_error(tmp_path: Path) -> None:
    vol = FakeVolume(host_dump_volume_name(_SID), capacity=4096)
    pool = FakePool(xml=_DIR_POOL_XML, volume=vol)
    conn = FakeHostDumpConn(pool=pool)
    store = FakeStore(head=_head_ok())
    err = CategorizedError("no VMCOREINFO", category=ErrorCategory.CONFIGURATION_ERROR)

    with pytest.raises(CategorizedError) as exc:
        _retrieve(conn, store, tmp_path, build_id_error=err).capture(_SID, CaptureMethod.HOST_DUMP)

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert vol.deleted  # AC6 + AC7: a no-build-id core still cleans up its volume


def test_host_dump_download_failure_still_cleans_up(tmp_path: Path) -> None:
    vol = FakeFailingVolume(host_dump_volume_name(_SID), capacity=4096)
    pool = FakePool(xml=_DIR_POOL_XML, volume=vol)
    conn = FakeHostDumpConn(pool=pool)
    store = FakeStore(head=None)

    with pytest.raises(CategorizedError):
        _retrieve(conn, store, tmp_path).capture(_SID, CaptureMethod.HOST_DUMP)

    assert vol.deleted  # AC7: finally deletes the volume on a forced download failure
    # AC5/AC7: the spool temp file is gone (no leftover under the worker temp dir).
    assert not list(tmp_path.glob("**/*host_dump*"))


def test_host_dump_missing_object_after_upload_is_infrastructure_failure(tmp_path: Path) -> None:
    vol = FakeVolume(host_dump_volume_name(_SID), capacity=4096)
    pool = FakePool(xml=_DIR_POOL_XML, volume=vol)
    conn = FakeHostDumpConn(pool=pool)
    store = FakeStore(head=None)  # head returns None despite a successful stream put

    with pytest.raises(CategorizedError) as exc:
        _retrieve(conn, store, tmp_path).capture(_SID, CaptureMethod.HOST_DUMP)

    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert vol.deleted


def test_host_dump_verifies_the_readback_checksum_when_present(tmp_path: Path) -> None:
    payload = b"CORE-BYTES"
    vol = FakeVolume(host_dump_volume_name(_SID), capacity=4096, payload=payload)
    pool = FakePool(xml=_DIR_POOL_XML, volume=vol)
    conn = FakeHostDumpConn(pool=pool)
    # head returns the matching checksum: the post-put verification passes.
    store = FakeStore(head=_head_ok(payload, checksum=_sha256_b64(payload)))

    out = _retrieve(conn, store, tmp_path).capture(_SID, CaptureMethod.HOST_DUMP)
    assert out.vmcore_build_id == "deadbeef"


def test_host_dump_readback_checksum_mismatch_is_infrastructure_failure(tmp_path: Path) -> None:
    payload = b"CORE-BYTES"
    vol = FakeVolume(host_dump_volume_name(_SID), capacity=4096, payload=payload)
    pool = FakePool(xml=_DIR_POOL_XML, volume=vol)
    conn = FakeHostDumpConn(pool=pool)
    # head returns a checksum that disagrees with the streamed core.
    store = FakeStore(head=_head_ok(payload, checksum=_sha256_b64(b"TAMPERED")))

    with pytest.raises(CategorizedError) as exc:
        _retrieve(conn, store, tmp_path).capture(_SID, CaptureMethod.HOST_DUMP)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert vol.deleted


def test_host_dump_stream_overrunning_the_ceiling_is_configuration_error(tmp_path: Path) -> None:
    # The volume reports a capacity under the (here tiny) ceiling so the pre-download check
    # passes, but the stream then delivers more than the ceiling — the sink must abort rather
    # than spool it all (the ADR-0094 §2 mid-download sanity bound against a lying host).
    oversize = b"x" * 100
    vol = FakeVolume(host_dump_volume_name(_SID), capacity=8, payload=oversize)
    pool = FakePool(xml=_DIR_POOL_XML, volume=vol)
    conn = FakeHostDumpConn(pool=pool)
    store = FakeStore(head=None)

    with pytest.raises(CategorizedError) as exc:
        _retrieve(conn, store, tmp_path, max_core_bytes=10).capture(_SID, CaptureMethod.HOST_DUMP)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert vol.deleted  # the over-streaming dump volume is still cleaned up
    assert not store.stream_requests  # never uploaded


def test_host_dump_spools_to_a_private_mode_file(tmp_path: Path) -> None:
    captured_mode: dict[str, int] = {}

    class _PermSnoopVolume(FakeVolume):
        def download(self, stream: FakeStream, offset: int, length: int, flags: int) -> None:
            super().download(stream, offset, length, flags)

    payload = b"sensitive-guest-RAM"
    vol = _PermSnoopVolume(host_dump_volume_name(_SID), capacity=4096, payload=payload)
    pool = FakePool(xml=_DIR_POOL_XML, volume=vol)
    conn = FakeHostDumpConn(pool=pool)

    class _PermStore(FakeStore):
        def put_stream(self, request: ArtifactStreamRequest) -> StoredArtifact:
            captured_mode["mode"] = stat.S_IMODE(os.stat(request.path).st_mode)
            return super().put_stream(request)

    store = _PermStore(head=_head_ok(payload))
    _retrieve(conn, store, tmp_path).capture(_SID, CaptureMethod.HOST_DUMP)
    # The spooled core (guest memory) must not be world/group readable.
    assert captured_mode["mode"] == 0o600
