"""Remote-libvirt ephemeral build-VM reaper (ADR-0100).

The reconciler's build-host upkeep consumes this provider port (the ``BuildVmReaper`` contract)
to reap ``kdive-build-<run_id>`` domains leaked by a worker/host crash that bypassed the
session's ``finally`` teardown. It lists the host's build domains (matched by the deterministic
name) and deletes one — destroy + undefine + delete its overlay — by name. The reconciler owns
the live-holder guard (the owning BUILD job's liveness, never elapsed time); this port is the
narrow libvirt I/O seam. The blocking libvirt calls run only under the ``live_vm`` gate;
name parsing + protocol conformance are unit-tested.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import libvirt

from kdive.providers.reaping import BuildVm
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.lifecycle.build_vm import (
    BUILD_DOMAIN_PREFIX,
    build_overlay_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.storage import delete_volume
from kdive.providers.remote_libvirt.transport import open_libvirt_protocol, remote_connection
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)

# The deterministic build-domain name carries the owning Run's UUID (ADR-0100). Anchored so a
# System domain (kdive-<uuid>, no "build-") can never match.
_BUILD_VM_RE = re.compile(
    r"^kdive-build-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def run_id_from_build_vm_name(name: str) -> UUID | None:
    """The owning Run UUID encoded in a build-VM domain name, or ``None`` if it does not match."""
    match = _BUILD_VM_RE.match(name)
    if match is None:
        return None
    try:
        return UUID(match.group(1))
    except ValueError:  # pragma: no cover - the regex already constrains the shape
        return None


class _Domain(Protocol):
    def name(self) -> str: ...
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...


class _ReaperConn(Protocol):
    def listAllDomains(self, flags: int = 0) -> list[_Domain]: ...  # noqa: N802 - binding name
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - binding name
    def storagePoolLookupByName(self, name: str) -> Any: ...  # noqa: N802 - binding name
    def close(self) -> None: ...


type OpenReaperConnection = Callable[[str], _ReaperConn]


def open_libvirt_reaper(uri: str) -> _ReaperConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return open_libvirt_protocol(uri)


class RemoteLibvirtBuildVmReaper:
    """List + delete leaked ephemeral build-VM domains on the remote host (the reconciler seam)."""

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
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtBuildVmReaper:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry)

    async def list_build_vms(self) -> list[BuildVm]:
        """List the host's ``kdive-build-*`` domains with their owning Run id (offloaded)."""
        return await asyncio.to_thread(self._list_blocking)

    async def delete_build_vm(self, domain_name: str) -> None:
        """Destroy+undefine the domain and delete its overlay; already-gone is not an error."""
        await asyncio.to_thread(self._delete_blocking, domain_name)

    def _list_blocking(self) -> list[BuildVm]:  # pragma: no cover - live_vm
        config = self._config_factory()
        with self._connection(config) as conn:
            vms: list[BuildVm] = []
            for domain in conn.listAllDomains(0):
                name = domain.name()
                if not name.startswith(BUILD_DOMAIN_PREFIX):
                    continue
                vms.append(BuildVm(domain_name=name, run_id=run_id_from_build_vm_name(name)))
            return vms

    def _delete_blocking(self, domain_name: str) -> None:  # pragma: no cover - live_vm
        config = self._config_factory()
        run_id = run_id_from_build_vm_name(domain_name)
        with self._connection(config) as conn:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError:
                domain = None
            if domain is not None:
                self._destroy_undefine(domain, domain_name)
            if run_id is not None:
                delete_volume(conn, config.storage_pool, build_overlay_volume_name(run_id))
            _log.info("reconciler: reaped leaked build VM %s", domain_name)

    @staticmethod
    def _destroy_undefine(domain: _Domain, domain_name: str) -> None:  # pragma: no cover - live_vm
        try:
            domain.destroy()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                _log.warning("build VM %s destroy failed during reap", domain_name)
        try:
            domain.undefine()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                _log.warning("build VM %s undefine failed during reap", domain_name)

    def _connection(self, config: RemoteLibvirtConfig) -> AbstractContextManager[_ReaperConn]:
        return remote_connection(
            config,
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )


__all__ = ["RemoteLibvirtBuildVmReaper", "run_id_from_build_vm_name"]
