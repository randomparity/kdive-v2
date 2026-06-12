"""Remote-libvirt host_dump orphaned-volume reaper (#301, ADR-0094).

The reconciler's stateless orphan-volume sweep consumes this provider port (the
``DumpVolumeReaper`` contract) to reap host_dump volumes orphaned by a non-graceful
worker/host crash that bypassed the capture's ``finally`` cleanup. It lists the storage
pool's dump volumes (matched by the deterministic ``kdive-host-dump-<system_id>.kdump``
name) with each volume's store mtime — read from the volume XML's ``<timestamps>/<mtime>``,
which libvirt populates for filesystem/dir-backed pools — and deletes one by name. The
reconciler owns both live-holder guards (no active capture job, mtime older than the grace
window); this port is the narrow libvirt I/O seam. The blocking libvirt calls run only under
the ``live_vm`` gate; orchestration + name/mtime parsing are unit-tested with fakes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.providers.reaping import DumpVolume
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.transport import open_libvirt_protocol, remote_connection
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)

# The deterministic dump-volume name carries the owning System's UUID (ADR-0094).
_DUMP_VOLUME_RE = re.compile(
    r"^kdive-host-dump-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\.kdump$"
)


class _Volume(Protocol):
    def name(self) -> str: ...
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802 - libvirt binding name
    def delete(self, flags: int = 0) -> int: ...


class _Pool(Protocol):
    def listAllVolumes(self, flags: int = 0) -> list[_Volume]: ...  # noqa: N802 - binding name
    def storageVolLookupByName(self, name: str) -> _Volume: ...  # noqa: N802 - binding name
    def refresh(self, flags: int = 0) -> int: ...


class _ReaperConn(Protocol):
    def storagePoolLookupByName(self, name: str) -> _Pool: ...  # noqa: N802 - binding name
    def close(self) -> None: ...


type OpenReaperConnection = Callable[[str], _ReaperConn]


def open_libvirt_reaper(uri: str) -> _ReaperConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return open_libvirt_protocol(uri)


def system_id_from_dump_volume_name(name: str) -> UUID | None:
    """The owning System UUID encoded in a dump-volume name, or ``None`` if it does not match."""
    match = _DUMP_VOLUME_RE.match(name)
    if match is None:
        return None
    try:
        return UUID(match.group(1))
    except ValueError:  # pragma: no cover - the regex already constrains the shape
        return None


def volume_mtime_epoch_s(volume_xml: str) -> float:
    """The volume's mtime (epoch seconds) from its XML ``<target>/<timestamps>/<mtime>``.

    libvirt populates ``<timestamps>`` for filesystem/dir-backed pools as ``sec.nsec``. A
    document without it (or a malformed one) yields ``0.0`` — epoch, which the reconciler's
    age check treats as old enough to consider for reaping, falling back to the active-capture
    guard so a missing timestamp never *protects* a true orphan from cleanup.
    """
    try:
        root = _safe_fromstring(volume_xml)
    except Exception:  # noqa: BLE001 - host-emitted XML; a parse failure reads as no timestamp
        return 0.0
    mtime = root.findtext("./target/timestamps/mtime")
    if mtime is None:
        return 0.0
    try:
        return float(mtime)
    except ValueError:
        return 0.0


class RemoteLibvirtDumpVolumeReaper:
    """List + delete host_dump volumes in the operator's storage pool (the reconciler seam)."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenReaperConnection = open_libvirt_reaper,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtDumpVolumeReaper:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry)

    async def list_dump_volumes(self) -> list[DumpVolume]:
        """List the storage pool's host_dump volumes with their store mtime (offloaded)."""
        return await asyncio.to_thread(self._list_blocking)

    async def delete_dump_volume(self, name: str) -> None:
        """Delete one dump volume by name; a volume already gone is not an error (offloaded)."""
        await asyncio.to_thread(self._delete_blocking, name)

    def _list_blocking(self) -> list[DumpVolume]:  # pragma: no cover - live_vm
        config = self._config_factory()
        with self._connection(config) as conn:
            pool = conn.storagePoolLookupByName(config.storage_pool)
            pool.refresh(0)
            volumes: list[DumpVolume] = []
            for volume in pool.listAllVolumes(0):
                name = volume.name()
                system_id = system_id_from_dump_volume_name(name)
                if system_id is None and not name.startswith("kdive-host-dump-"):
                    continue
                volumes.append(
                    DumpVolume(
                        name=name,
                        system_id=system_id,
                        mtime_epoch_s=volume_mtime_epoch_s(volume.XMLDesc(0)),
                    )
                )
            return volumes

    def _delete_blocking(self, name: str) -> None:  # pragma: no cover - live_vm
        config = self._config_factory()
        with self._connection(config) as conn:
            pool = conn.storagePoolLookupByName(config.storage_pool)
            try:
                volume = pool.storageVolLookupByName(name)
            except libvirt.libvirtError:
                return  # already gone (a live capture's finally beat the reap) — idempotent
            volume.delete(0)
            _log.info("reconciler: deleted orphaned host_dump volume %s", name)

    def _connection(self, config: RemoteLibvirtConfig) -> AbstractContextManager[_ReaperConn]:
        return remote_connection(
            config,
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )


__all__ = [
    "RemoteLibvirtDumpVolumeReaper",
    "system_id_from_dump_volume_name",
    "volume_mtime_epoch_s",
]
