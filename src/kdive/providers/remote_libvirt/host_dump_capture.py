"""Remote-libvirt host-side vmcore capture workflow."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID

import libvirt
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactStreamRequest, StoredArtifact
from kdive.providers.ports import CaptureOutput
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig
from kdive.providers.remote_libvirt.retrieve_common import (
    OWNER_KIND,
    RETENTION,
    TENANT,
    CoreBuildIdFromFile,
    CoreDmesgFromFile,
    OpenRetrieveConnection,
    StorePort,
    connection,
    lookup,
    persist_redacted,
)
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend

_log = logging.getLogger(__name__)

DMESG_UNAVAILABLE = (
    b"[kdive] dmesg could not be extracted from this host_dump core "
    b"(kernel debuginfo required); see the crash postmortem for the kernel log\n"
)
DIR_POOL_TYPES = frozenset({"dir", "fs", "netfs"})
SPOOL_CHUNK_BYTES = 8 * 1024 * 1024


def host_dump_volume_name(system_id: UUID) -> str:
    """The deterministic per-System dump-volume filename inside the storage pool."""
    return f"kdive-host-dump-{system_id}.kdump"


def open_core_program(core: Path) -> Any:  # pragma: no cover - live_vm (drgn)
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
    """The crashed kernel's GNU build-id from a compressed-kdump core's VMCOREINFO."""
    prog = open_core_program(core)
    vmcoreinfo = bytes(prog["VMCOREINFO"].value_())
    match = re.search(rb"BUILD-ID=([0-9a-f]{40})", vmcoreinfo)
    if match is None:
        raise CategorizedError(
            "host_dump core carries no VMCOREINFO BUILD-ID line; cannot verify provenance",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return match.group(1).decode("ascii")


def read_core_dmesg_from_file(core: Path) -> bytes:  # pragma: no cover - live_vm (drgn)
    """The kernel log buffer from an ELF/kdump core (drgn ``get_dmesg``)."""
    from drgn.helpers.linux.printk import (  # noqa: PLC0415  # ty: ignore[unresolved-import]
        get_dmesg,
    )

    prog = open_core_program(core)
    try:
        return get_dmesg(prog)
    except Exception as exc:
        raise CategorizedError(
            "could not extract dmesg from the host_dump core; the printk ring buffer needs the "
            "guest kernel's debuginfo, which is not loaded at capture time",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc


class HostDumpOptions(NamedTuple):
    core_build_id_from_file: CoreBuildIdFromFile
    core_dmesg_from_file: CoreDmesgFromFile
    dump_format: int
    max_core_bytes: int


class HostDumpCapturer:
    """Host-side libvirt core dump, stream download, and object-store upload."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig],
        open_connection: OpenRetrieveConnection,
        store_factory: Callable[[], StorePort],
        secret_backend_factory: Callable[[], SecretBackend],
        pki_base_dir: Path | None,
        options: HostDumpOptions,
    ) -> None:
        self._secret_registry = secret_registry
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._store_factory = store_factory
        self._secret_backend_factory = secret_backend_factory
        self._pki_base_dir = pki_base_dir
        self._options = options

    def capture(self, system_id: UUID) -> CaptureOutput:
        """Host-side core-dump -> storage-pool volume -> stream download -> upload."""
        config = self._config_factory()
        with connection(
            config, self._secret_backend_factory, self._open_connection, self._pki_base_dir
        ) as conn:
            domain = lookup(conn, domain_name_for(system_id))
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
        pool_xml = pool.XMLDesc(0)
        pool_type, target = pool_type_and_target(pool_xml)
        if pool_type not in DIR_POOL_TYPES or target is None:
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
            domain.coreDumpWithFormat(path, self._options.dump_format, flags)
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
        """Reject an over-ceiling volume before any download."""
        capacity = int(volume.info()[1])
        if capacity > self._options.max_core_bytes:
            raise CategorizedError(
                "host_dump core exceeds the single-PUT 5 GiB ceiling",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id), "capacity_bytes": capacity},
            )

    def _download_to_file(self, conn: Any, volume: Any, spool: Path, system_id: UUID) -> None:
        """Spool the volume to a 0600 temp file; abort+raise if the stream overruns."""
        stream = conn.newStream(0)
        written = 0
        try:
            fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:

                def _sink(_stream: Any, data: bytes, _opaque: Any) -> None:
                    nonlocal written
                    written += len(data)
                    if written > self._options.max_core_bytes:
                        raise CategorizedError(
                            "host_dump stream exceeded the 5 GiB ceiling mid-download",
                            category=ErrorCategory.CONFIGURATION_ERROR,
                            details={"system_id": str(system_id), "streamed_bytes": written},
                        )
                    handle.write(data)

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
        build_id = self._options.core_build_id_from_file(spool)
        dmesg = self._dmesg_best_effort(spool, system_id)
        sha256_b64 = file_sha256_b64(spool)
        raw = self._stream_put(system_id, spool, sha256_b64)
        self._verify_stored(raw.key, sha256_b64, system_id)
        redacted = persist_redacted(
            self._store_factory,
            self._secret_registry,
            system_id,
            CaptureMethod.HOST_DUMP,
            dmesg,
        )
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=build_id)

    def _dmesg_best_effort(self, spool: Path, system_id: UUID) -> bytes:
        try:
            return self._options.core_dmesg_from_file(spool)
        except CategorizedError as exc:
            if exc.category is ErrorCategory.MISSING_DEPENDENCY:
                raise
            _log.warning(
                "host_dump dmesg extraction failed for system %s; persisting a placeholder "
                "(core + build-id captured): %s",
                system_id,
                exc,
            )
            return DMESG_UNAVAILABLE

    def _stream_put(self, system_id: UUID, spool: Path, sha256_b64: str) -> StoredArtifact:
        return self._store_factory().put_stream(
            ArtifactStreamRequest(
                tenant=TENANT,
                owner_kind=OWNER_KIND,
                owner_id=str(system_id),
                name=f"vmcore-{CaptureMethod.HOST_DUMP.value}",
                path=spool,
                sha256_b64=sha256_b64,
                sensitivity=Sensitivity.SENSITIVE,
                retention_class=RETENTION,
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


def pool_type_and_target(pool_xml: str) -> tuple[str | None, str | None]:
    """Return ``(pool_type, target_path)`` from a storage-pool XML (tolerant parse)."""
    try:
        root: ET.Element = _safe_fromstring(pool_xml)
    except ET.ParseError:
        return None, None
    target = root.findtext("./target/path")
    return root.get("type"), target


def file_sha256_b64(path: Path) -> str:
    """Stream a file through sha256 and return the base64 digest S3 signs."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(SPOOL_CHUNK_BYTES), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")
