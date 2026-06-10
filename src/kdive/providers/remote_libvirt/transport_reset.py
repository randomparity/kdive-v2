"""Remote-libvirt transport reset: re-arm a dead worker's gdbstub (#216, ADR-0086).

When the reconciler detaches a stale ``live`` DebugSession, this resetter frees the System's
single-client gdbstub so the next attach is not blocked by the dead worker's lingering
connection (ADR-0079). It self-selects: only a ``gdbstub`` transport whose handle host equals
the operator ``gdb_addr`` and that carries a domain name is re-armed; everything else is a
no-op. The re-arm is the explicit stop-then-rearm (``gdbserver none`` then
``gdbserver tcp::<port>``) over the ``qemu+tls`` monitor, closing the holding connection
deterministically (ADR-0083 host policy; ADR-0077 connection lifecycle). The monitor call runs
only under the ``live_vm`` gate; orchestration + self-selection are unit-tested with fakes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import TransportHandleData
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)
_GDBSTUB = "gdbstub"


class _Domain(Protocol):
    def qemuMonitorCommand(self, cmd: str, flags: int) -> str: ...  # noqa: N802 - libvirt name


class _ResetConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenResetConnection = Callable[[str], _ResetConn]


def open_libvirt_reset(uri: str) -> _ResetConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]


def _real_rearm(domain: _Domain, port: int) -> None:  # pragma: no cover - live_vm
    """Stop-then-rearm the gdbstub over the QEMU monitor (HMP), dropping the stale client."""
    import libvirt_qemu

    hmp = libvirt_qemu.VIR_DOMAIN_QEMU_MONITOR_COMMAND_HMP
    domain.qemuMonitorCommand("gdbserver none", hmp)
    domain.qemuMonitorCommand(f"gdbserver tcp::{port}", hmp)


class RemoteLibvirtTransportResetter:
    """Re-arm a dead worker's remote gdbstub so the freed port no longer blocks re-attach."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenResetConnection = open_libvirt_reset,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        rearm: Callable[[_Domain, int], None] = _real_rearm,
        pki_base_dir: Path | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._rearm = rearm
        self._pki_base_dir = pki_base_dir

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtTransportResetter:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry)

    async def reset(
        self, *, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> None:
        """Re-arm the gdbstub if this is a matching remote gdbstub session; else no-op.

        Raises:
            CategorizedError: ``TRANSPORT_FAILURE`` if the monitor re-arm errors.
        """
        port = self._port_if_ours(transport, transport_handle, domain_name)
        if port is None or domain_name is None:
            return
        await asyncio.to_thread(self._rearm_blocking, domain_name, port)
        _log.info("reconciler: re-armed remote gdbstub for domain %s (port %d)", domain_name, port)

    def _port_if_ours(
        self, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> int | None:
        if transport != _GDBSTUB:
            return None
        if transport_handle is None:
            _log.info("reconciler: gdbstub session has no handle; skipping reset")
            return None
        try:
            data = TransportHandleData.decode(transport_handle)
        except CategorizedError:
            _log.info("reconciler: undecodable transport handle; skipping reset")
            return None
        config = self._config_factory()
        if data.kind != _GDBSTUB or data.host != config.gdb_addr:
            return None  # a local loopback gdbstub, or not our gdb_addr — not ours to reset
        if domain_name is None:
            _log.info("reconciler: remote gdbstub session has no domain_name; cannot reset")
            return None
        return data.port

    def _rearm_blocking(self, domain_name: str, port: int) -> None:
        with self._connection() as conn:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    f"looking up domain {domain_name!r} for gdbstub reset failed",
                    category=ErrorCategory.TRANSPORT_FAILURE,
                ) from exc
            try:
                self._rearm(domain, port)
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    "re-arming the remote gdbstub failed",
                    category=ErrorCategory.TRANSPORT_FAILURE,
                    details={"port": port},
                ) from exc

    def _connection(self) -> AbstractContextManager[_ResetConn]:
        return remote_connection(
            self._config_factory(),
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )


__all__ = ["RemoteLibvirtTransportResetter"]
