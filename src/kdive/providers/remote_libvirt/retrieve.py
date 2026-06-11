"""Remote-libvirt Retrieve plane: two-phase vmcore capture + crash postmortem (ADR-0084).

`capture()` runs after a crash against a System whose guest must have rebooted out of the
kdump capture kernel: it waits out a still-rebooting agent (readiness), inspects the local
core (digest/size/build-id + a bounded inline redacted dmesg), mints a single presigned PUT
for a deterministic key, runs the in-guest upload through the registered-URL artifact channel
(ADR-0078 §2), and references the uploaded object via `head` (presence + etag; the signed
checksum is the integrity binding). The redacted dmesg is redacted again worker-side and
persisted inline. `run_crash_postmortem()` delegates to the shared `debug_common` helper.
host_dump (ADR-0094) is a second `capture()` branch: it host-side core-dumps the crashed
guest's memory (memory-only, compressed kdump) into a storage-pool volume, streams it back
over the same TLS connection, spools it to a worker temp file, extracts the build-id +
redacts dmesg at constant memory over the spooled file, and uploads — then deletes the temp
file and the host volume in a `finally`. All host/S3/clock/drgn seams are injected.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, NamedTuple, Protocol
from uuid import UUID

import libvirt
from defusedxml.ElementTree import fromstring as _safe_fromstring

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
    artifact_key,
)
from kdive.providers.debug_common.crash_postmortem import (
    FetchObject,
    ReadBuildId,
    RunCrash,
    default_fetch_object,
    default_read_vmcore_build_id,
    default_run_crash,
)
from kdive.providers.debug_common.crash_postmortem import (
    run_crash_postmortem as _run_crash_postmortem,
)
from kdive.providers.ports import CaptureOutput, CrashOutput
from kdive.providers.remote_libvirt.artifact_channel import InTargetArtifactChannel
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.guest_agent import (
    AgentCommand,
    AgentExecResult,
    GuestAgentExec,
    qemu_agent_command,
)
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env
from kdive.store.objectstore import object_store_from_env

_HELPER = "/usr/local/sbin/kdive-capture-vmcore"
_TENANT = "remote-libvirt"
_RETENTION = "vmcore"
_OWNER_KIND = "systems"
# One object + one checksum; lifetime must cover the in-guest upload of a multi-hundred-MB core.
_DEFAULT_PUT_EXPIRY_S = 3600
# S3 single-PUT ceiling (ADR-0048); larger cores are a multipart follow-up.
_MAX_CORE_BYTES = 5 * 1024**3
_DEFAULT_READINESS_TIMEOUT_S = 300.0
_DEFAULT_READINESS_POLL_S = 2.0
# The inspect command hashes the whole core in-guest; 120s comfortably covers a sha256 of
# the 5 GiB ceiling (~10s at commodity disk/CPU rates). GuestAgentExec folds a command that
# does not exit within this bound into TRANSPORT_FAILURE, which _await_inspect treats as
# "still rebooting" — acceptable because a working inspect finishes well inside the bound, so
# a TRANSPORT_FAILURE in practice means an unreachable agent, not a slow hash.
_DEFAULT_INSPECT_TIMEOUT_S = 120.0
_DEFAULT_UPLOAD_TIMEOUT_S = 1800.0
# An unreachable agent during readiness is "still rebooting out of the kdump kernel". A
# non-rebooting CategorizedError (a malformed reply -> INFRASTRUCTURE_FAILURE) is NOT in this
# set, so _await_inspect re-raises it immediately instead of spinning the readiness window.
_AGENT_REBOOTING = frozenset({ErrorCategory.TRANSPORT_FAILURE})

# A dir/filesystem storage pool is the only type host_dump can dump-into-then-discover
# (ADR-0094): an LVM/RBD/iSCSI pool has no directory to write a file into.
_DIR_POOL_TYPES = frozenset({"dir", "fs", "netfs"})
# The libvirt dump-format token the host's domainCapabilities must advertise for KDUMP_ZLIB.
_KDUMP_ZLIB_FORMAT_TOKEN = "kdump-zlib"
# Read the spooled core in fixed chunks for the sha256 pass so a multi-GB core never lands
# whole in RAM (ADR-0094 constant-memory requirement).
_SPOOL_CHUNK_BYTES = 8 * 1024 * 1024


def host_dump_volume_name(system_id: UUID) -> str:
    """The deterministic per-System dump-volume filename inside the storage pool.

    Deterministic so a stale orphan from a crashed prior capture collides with — and is
    deleted before — the next capture's dump, and so the reconciler sweep can match a dump
    volume back to its owning System (ADR-0094).
    """
    return f"kdive-host-dump-{system_id}.kdump"


class _CoreInfo(NamedTuple):
    sha256: str
    size_bytes: int
    build_id: str
    dmesg: bytes


class _StorePort(Protocol):
    def presign_put(self, request: PresignPutRequest) -> PresignedUpload: ...
    def head(self, key: str) -> HeadResult | None: ...
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...
    def put_stream(self, request: ArtifactStreamRequest) -> StoredArtifact: ...


class _Domain(Protocol):
    def name(self) -> str: ...


class _RetrieveConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


class _AgentExec(Protocol):
    def run(self, domain: Any, argv: list[str]) -> AgentExecResult: ...


type OpenRetrieveConnection = Callable[[str], _RetrieveConn]
type AgentExecFactory = Callable[[float], _AgentExec]
type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]
# Read the crashed kernel's build-id / dmesg from a spooled compressed-kdump core file.
# Production defaults open the core with drgn (which parses makedumpfile containers); both
# run only under the live_vm gate, so unit tests inject fakes.
type CoreBuildIdFromFile = Callable[[Path], str]
type CoreDmesgFromFile = Callable[[Path], bytes]


def open_libvirt_capture(uri: str) -> _RetrieveConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]


def _open_core_program(core: Path) -> Any:  # pragma: no cover - live_vm (drgn)
    try:
        import drgn  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided
    except ImportError as exc:
        raise CategorizedError(
            "drgn is not installed on this worker host; host_dump build-id/dmesg needs it",
            category=ErrorCategory.MISSING_DEPENDENCY,
        ) from exc
    prog = drgn.Program()
    prog.set_core_dump(os.fspath(core))
    return prog


def read_core_build_id_from_file(core: Path) -> str:  # pragma: no cover - live_vm (drgn)
    """The crashed kernel's GNU build-id from a compressed-kdump core's VMCOREINFO (drgn).

    drgn parses the makedumpfile container and exposes its VMCOREINFO; the ``BUILD-ID=`` line
    is the provenance binding ``CaptureOutput.vmcore_build_id`` requires. A core with no
    VMCOREINFO build-id is rejected (no fabricated empty id).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the core carries no VMCOREINFO
            BUILD-ID line; ``MISSING_DEPENDENCY`` when drgn is absent.
    """
    prog = _open_core_program(core)
    vmcoreinfo = bytes(prog["VMCOREINFO"].value_())
    match = re.search(rb"BUILD-ID=([0-9a-f]{40})", vmcoreinfo)
    if match is None:
        raise CategorizedError(
            "host_dump core carries no VMCOREINFO BUILD-ID line; cannot verify provenance",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return match.group(1).decode("ascii")


def read_core_dmesg_from_file(core: Path) -> bytes:  # pragma: no cover - live_vm (drgn)
    """The kernel log buffer from a compressed-kdump core (drgn ``get_dmesg``).

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` when drgn is absent.
    """
    from drgn.helpers.linux.printk import (  # noqa: PLC0415  # ty: ignore[unresolved-import]
        get_dmesg,
    )

    prog = _open_core_program(core)
    return get_dmesg(prog)


class RemoteLibvirtRetrieve:
    """The realized remote `Retriever` + `CrashPostmortem` (ADR-0084)."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenRetrieveConnection = open_libvirt_capture,
        store_factory: Callable[[], _StorePort] = object_store_from_env,
        agent_command: AgentCommand = qemu_agent_command,
        agent_exec_factory: AgentExecFactory | None = None,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
        put_expiry_s: int = _DEFAULT_PUT_EXPIRY_S,
        readiness_timeout_s: float = _DEFAULT_READINESS_TIMEOUT_S,
        readiness_poll_s: float = _DEFAULT_READINESS_POLL_S,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
        fetch_object: FetchObject = default_fetch_object,
        read_build_id: ReadBuildId = default_read_vmcore_build_id,
        run_crash: RunCrash = default_run_crash,
        core_build_id_from_file: CoreBuildIdFromFile = read_core_build_id_from_file,
        core_dmesg_from_file: CoreDmesgFromFile = read_core_dmesg_from_file,
        host_dump_format: int = libvirt.VIR_DOMAIN_CORE_DUMP_FORMAT_KDUMP_ZLIB,
        max_core_bytes: int = _MAX_CORE_BYTES,
    ) -> None:
        self._secret_registry = secret_registry
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._store_factory = store_factory
        self._agent_command = agent_command
        self._agent_exec_factory = agent_exec_factory or self._default_agent_exec
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir
        self._put_expiry_s = put_expiry_s
        self._readiness_timeout_s = readiness_timeout_s
        self._readiness_poll_s = readiness_poll_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._fetch_object = fetch_object
        self._read_build_id = read_build_id
        self._run_crash = run_crash
        self._core_build_id_from_file = core_build_id_from_file
        self._core_dmesg_from_file = core_dmesg_from_file
        self._host_dump_format = host_dump_format
        self._max_core_bytes = max_core_bytes

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtRetrieve:
        """Build from the shared worker env; opens no connection and mints no URL here."""
        return cls(secret_registry=secret_registry)

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        """Capture a vmcore: kdump (in-guest, two-phase) or host_dump (host-side, ADR-0094).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unsupported method, an
                over-ceiling core, a host lacking kdump-zlib, a non-dir pool, or a core
                with no VMCOREINFO build-id; ``READINESS_FAILURE`` when the guest never
                becomes reachable or carries no core; ``TRANSPORT_FAILURE`` for an agent
                fault outside the readiness window; ``INFRASTRUCTURE_FAILURE`` for an upload
                or download failure, a malformed reply, or an object absent after a
                success-reporting upload.
        """
        if method is CaptureMethod.HOST_DUMP:
            return self._capture_host_dump(system_id)
        if method is not CaptureMethod.KDUMP:
            raise CategorizedError(
                "remote-libvirt capture supports only the kdump and host_dump methods",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"method": method.value},
            )
        config = self._config_factory()
        raw_key = artifact_key(_TENANT, _OWNER_KIND, str(system_id), f"vmcore-{method.value}")
        with self._connection(config) as conn:
            domain = self._lookup(conn, domain_name_for(system_id))
            info = self._await_inspect(domain, system_id)
            upload = self._store_factory().presign_put(
                PresignPutRequest(
                    key=raw_key,
                    sha256=info.sha256,
                    size_bytes=info.size_bytes,
                    sensitivity=Sensitivity.SENSITIVE,
                    retention_class=_RETENTION,
                    expires_in=self._put_expiry_s,
                )
            )
            self._upload(domain, system_id, upload)
        raw = self._reference(raw_key, info.sha256, system_id)
        redacted = self._persist_redacted(system_id, method, info.dmesg)
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=info.build_id)

    def _capture_host_dump(self, system_id: UUID) -> CaptureOutput:
        """Host-side core-dump → storage-pool volume → stream download → upload (ADR-0094).

        Preflights (kdump-zlib host support, dir pool, 5 GiB ceiling) fire before paying a
        dump/stream; the spooled temp file and the host volume are both removed in a
        ``finally`` on every exit path.
        """
        config = self._config_factory()
        with self._connection(config) as conn:
            domain = self._lookup(conn, domain_name_for(system_id))
            self._preflight_host_kdump_zlib(conn)
            pool = self._lookup_pool(conn, config.storage_pool)
            pool_dir = self._preflight_pool_dir(pool, config.storage_pool)
            vol_name = host_dump_volume_name(system_id)
            self._delete_stale_volume(pool, vol_name)
            self._core_dump(domain, str(pool_dir / vol_name), system_id)
            return self._stream_and_store(conn, pool, vol_name, system_id)

    def _stream_and_store(
        self, conn: Any, pool: Any, vol_name: str, system_id: UUID
    ) -> CaptureOutput:
        pool.refresh(0)
        volume = self._resolve_volume(pool, vol_name, system_id)
        self._enforce_ceiling(volume, system_id)
        spool = Path(tempfile.mkdtemp(prefix="kdive-host-dump-")) / vol_name
        try:
            self._download_to_file(conn, volume, spool, system_id)
            return self._store_core(system_id, spool)
        finally:
            spool.unlink(missing_ok=True)
            with contextlib.suppress(Exception):
                spool.parent.rmdir()
            self._delete_volume(volume)

    def _preflight_host_kdump_zlib(self, conn: Any) -> None:
        """Fail with CONFIGURATION_ERROR if the host cannot emit a kdump-zlib memory dump."""
        try:
            caps_xml = conn.getDomainCapabilities()
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote host domain-capabilities probe failed for host_dump",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        if _KDUMP_ZLIB_FORMAT_TOKEN not in _supported_dump_formats(caps_xml):
            raise CategorizedError(
                "remote host does not advertise the kdump-zlib core-dump format; "
                "host_dump needs a libvirt+QEMU that supports compressed memory-only dumps",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"required_format": _KDUMP_ZLIB_FORMAT_TOKEN},
            )

    @staticmethod
    def _lookup_pool(conn: Any, pool_name: str) -> Any:
        try:
            return conn.storagePoolLookupByName(pool_name)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote storage-pool lookup failed for host_dump",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"storage_pool": pool_name},
            ) from exc

    @staticmethod
    def _preflight_pool_dir(pool: Any, pool_name: str) -> Path:
        """Return the pool's target directory, or fail on a non-dir/filesystem pool."""
        pool_type, target = _pool_type_and_target(pool.XMLDesc(0))
        if pool_type not in _DIR_POOL_TYPES or target is None:
            raise CategorizedError(
                "remote storage_pool is not a filesystem/dir pool; host_dump requires one",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"storage_pool": pool_name, "pool_type": pool_type or "unknown"},
            )
        return Path(target)

    def _delete_stale_volume(self, pool: Any, vol_name: str) -> None:
        """Delete a prior orphaned dump volume of the same deterministic name, if present."""
        try:
            stale = pool.storageVolLookupByName(vol_name)
        except libvirt.libvirtError:
            return
        self._delete_volume(stale)

    def _core_dump(self, domain: Any, path: str, system_id: UUID) -> None:
        flags = libvirt.VIR_DUMP_MEMORY_ONLY
        try:
            domain.coreDumpWithFormat(path, self._host_dump_format, flags)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote host_dump core-dump failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            ) from exc

    @staticmethod
    def _resolve_volume(pool: Any, vol_name: str, system_id: UUID) -> Any:
        try:
            return pool.storageVolLookupByName(vol_name)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "host_dump volume not found after the dump + pool refresh",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "volume": vol_name},
            ) from exc

    def _enforce_ceiling(self, volume: Any, system_id: UUID) -> None:
        """Reject an over-ceiling volume before any download; still clean the dump up."""
        capacity = int(volume.info()[1])
        if capacity > self._max_core_bytes:
            self._delete_volume(volume)
            raise CategorizedError(
                "host_dump core exceeds the single-PUT 5 GiB ceiling",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id), "capacity_bytes": capacity},
            )

    def _download_to_file(self, conn: Any, volume: Any, spool: Path, system_id: UUID) -> None:
        """Spool the volume to a 0600 temp file; abort+raise if the stream overruns the ceiling.

        The pre-download ceiling check is the primary bound (against the volume's reported
        capacity), but a host that under-reports capacity could still stream past it — so the
        sink also counts bytes and aborts the moment the spool would exceed the ceiling,
        capping worker disk/OOM exposure to a lying or racing host (ADR-0094 §2 sanity check).
        """
        stream = conn.newStream(0)
        written = 0

        def _sink(_stream: Any, data: bytes, _opaque: Any) -> None:
            nonlocal written
            written += len(data)
            if written > self._max_core_bytes:
                raise CategorizedError(
                    "host_dump stream exceeded the 5 GiB ceiling mid-download",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"system_id": str(system_id), "streamed_bytes": written},
                )
            handle.write(data)

        try:
            fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                volume.download(stream, 0, 0, 0)
                stream.recvAll(_sink, None)
            stream.finish()
        except CategorizedError:
            with contextlib.suppress(Exception):
                stream.abort()
            raise
        except (libvirt.libvirtError, OSError, RuntimeError) as exc:
            with contextlib.suppress(Exception):
                stream.abort()
            raise CategorizedError(
                "host_dump stream download failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            ) from exc

    def _store_core(self, system_id: UUID, spool: Path) -> CaptureOutput:
        build_id = self._core_build_id_from_file(spool)
        dmesg = self._core_dmesg_from_file(spool)
        sha256_b64 = _file_sha256_b64(spool)
        raw = self._stream_put(system_id, spool, sha256_b64)
        self._verify_stored(raw.key, sha256_b64, system_id)
        redacted = self._persist_redacted(system_id, CaptureMethod.HOST_DUMP, dmesg)
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=build_id)

    def _stream_put(self, system_id: UUID, spool: Path, sha256_b64: str) -> StoredArtifact:
        return self._store_factory().put_stream(
            ArtifactStreamRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=str(system_id),
                name=f"vmcore-{CaptureMethod.HOST_DUMP.value}",
                path=spool,
                sha256_b64=sha256_b64,
                sensitivity=Sensitivity.SENSITIVE,
                retention_class=_RETENTION,
            )
        )

    def _verify_stored(self, raw_key: str, sha256_b64: str, system_id: UUID) -> None:
        head = self._store_factory().head(raw_key)
        if head is None:
            raise CategorizedError(
                "stored host_dump core is absent after a success-reporting put",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "key": raw_key},
            )
        if head.checksum_sha256 is not None and head.checksum_sha256 != sha256_b64:
            raise CategorizedError(
                "stored host_dump core checksum does not match the streamed core",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "key": raw_key},
            )

    @staticmethod
    def _delete_volume(volume: Any) -> None:
        with contextlib.suppress(Exception):
            volume.delete(0)

    def run_crash_postmortem(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        """Delegate to the provider-neutral worker-side crash postmortem (ADR-0084)."""
        return _run_crash_postmortem(
            vmcore_ref=vmcore_ref,
            debuginfo_ref=debuginfo_ref,
            expected_build_id=expected_build_id,
            commands=commands,
            fetch_object=self._fetch_object,
            read_build_id=self._read_build_id,
            run_crash=self._run_crash,
            secret_registry=self._secret_registry,
        )

    def _await_inspect(self, domain: _Domain, system_id: UUID) -> _CoreInfo:
        agent_exec = self._agent_exec_factory(_DEFAULT_INSPECT_TIMEOUT_S)
        deadline = self._monotonic() + self._readiness_timeout_s
        while True:
            try:
                result = agent_exec.run(domain, [_HELPER, "inspect"])
            except CategorizedError as exc:
                if exc.category not in _AGENT_REBOOTING:
                    raise
                if self._monotonic() >= deadline:
                    raise self._readiness_failure(
                        system_id, "guest agent never came back within the capture window"
                    ) from exc
                self._sleep(self._readiness_poll_s)
                continue
            return self._parse_inspect(result, system_id)

    def _parse_inspect(self, result: AgentExecResult, system_id: UUID) -> _CoreInfo:
        if result.exit_status != 0:
            raise CategorizedError(
                "in-guest vmcore inspect exited non-zero",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "exit_status": result.exit_status},
            )
        try:
            payload = json.loads(result.stdout.decode("utf-8", "replace"))
            present = bool(payload["present"])
            sha256 = str(payload["sha256"])
            size_bytes = int(payload["size_bytes"])
            build_id = str(payload["build_id"])
            dmesg = base64.b64decode(payload["dmesg_b64"])
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise CategorizedError(
                "guest vmcore inspect returned a malformed reply",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            ) from exc
        if not present:
            raise self._readiness_failure(system_id, "no kdump core in the guest's dump storage")
        if size_bytes > _MAX_CORE_BYTES:
            raise CategorizedError(
                "captured core exceeds the single-PUT 5 GiB ceiling",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id), "size_bytes": size_bytes},
            )
        return _CoreInfo(sha256=sha256, size_bytes=size_bytes, build_id=build_id, dmesg=dmesg)

    def _upload(self, domain: _Domain, system_id: UUID, upload: PresignedUpload) -> None:
        argv = [_HELPER, "upload", "--url", upload.url]
        for key, value in upload.required_headers.items():
            argv += ["--header", f"{key}:{value}"]
        channel = InTargetArtifactChannel(
            registry=self._secret_registry,
            agent_exec=self._agent_exec_factory(_DEFAULT_UPLOAD_TIMEOUT_S),
            store_factory=self._store_factory,
            scope=object(),
        )
        output = channel.exec_with_capability(
            domain,
            capability_url=upload.url,
            argv=argv,
            owner_kind=_OWNER_KIND,
            owner_id=str(system_id),
        )
        if output.result.exit_status != 0:
            raise CategorizedError(
                "in-guest vmcore upload exited non-zero",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "exit_status": output.result.exit_status},
            )

    def _reference(self, raw_key: str, sha256: str, system_id: UUID) -> StoredArtifact:
        head = self._store_factory().head(raw_key)
        if head is None:
            raise CategorizedError(
                "uploaded vmcore is absent after a success-reporting upload",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "key": raw_key},
            )
        if head.checksum_sha256 is not None and head.checksum_sha256 != sha256:
            raise CategorizedError(
                "uploaded vmcore checksum does not match the inspected core",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id), "key": raw_key},
            )
        return StoredArtifact(raw_key, head.etag, Sensitivity.SENSITIVE, _RETENTION)

    def _persist_redacted(
        self, system_id: UUID, method: CaptureMethod, dmesg: bytes
    ) -> StoredArtifact:
        text = dmesg.decode("utf-8", "replace")
        redacted = Redactor(registry=self._secret_registry).redact_text(text)
        return self._store_factory().put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind=_OWNER_KIND,
                owner_id=str(system_id),
                name=f"vmcore-{method.value}-redacted",
                data=redacted.encode("utf-8"),
                sensitivity=Sensitivity.REDACTED,
                retention_class=_RETENTION,
            )
        )

    def _default_agent_exec(self, timeout_s: float) -> GuestAgentExec:
        return GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({_HELPER}),
            timeout_s=timeout_s,
            sleep=self._sleep,
            monotonic=self._monotonic,
        )

    def _connection(self, config: RemoteLibvirtConfig) -> AbstractContextManager[_RetrieveConn]:
        return remote_connection(
            config,
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )

    @staticmethod
    def _lookup(conn: _RetrieveConn, domain_name: str) -> _Domain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote domain lookup failed for capture",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"domain": domain_name},
            ) from exc

    @staticmethod
    def _readiness_failure(system_id: UUID, reason: str) -> CategorizedError:
        return CategorizedError(
            reason,
            category=ErrorCategory.READINESS_FAILURE,
            details={"system_id": str(system_id)},
        )


def _supported_dump_formats(caps_xml: str) -> frozenset[str]:
    """The core-dump format tokens the host advertises in domainCapabilities (tolerant parse).

    The XML is host-emitted (untrusted), so it is parsed with ``defusedxml``; a malformed
    document yields the empty set, which the caller treats as "kdump-zlib unsupported" — a
    fail-closed CONFIGURATION_ERROR rather than an unreadable dump.
    """
    try:
        root: ET.Element = _safe_fromstring(caps_xml)
    except ET.ParseError:
        return frozenset()
    return frozenset(
        value.text.strip()
        for value in root.findall("./dump/enum[@name='format']/value")
        if value.text
    )


def _pool_type_and_target(pool_xml: str) -> tuple[str | None, str | None]:
    """Return ``(pool_type, target_path)`` from a storage-pool XML (tolerant parse).

    Host-emitted XML is parsed with ``defusedxml``; a malformed document yields
    ``(None, None)``, which the caller rejects as a non-dir pool (fail-closed).
    """
    try:
        root: ET.Element = _safe_fromstring(pool_xml)
    except ET.ParseError:
        return None, None
    target = root.findtext("./target/path")
    return root.get("type"), target


def _file_sha256_b64(path: Path) -> str:
    """Stream a file through sha256 (constant memory); return the base64 digest S3 signs.

    Base64 is the form ``ChecksumSHA256`` takes on the PUT and that ``head`` reads back, so
    the value binds the upload integrity and the post-put verification without re-encoding.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_SPOOL_CHUNK_BYTES), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


__all__ = [
    "RemoteLibvirtRetrieve",
    "host_dump_volume_name",
    "read_core_build_id_from_file",
    "read_core_dmesg_from_file",
]
