"""Remote-libvirt Discovery plane over qemu+tls (ADR-0076, ADR-0077).

Enumerates the remote host over an injected mutual-TLS connection (unit tests never
touch a real host; the real ``libvirt.open`` adapter is the production opener) and
advertises arch/cpu/memory, the gdbstub transport, the connect URI + TLS secret refs,
and the per-host concurrent-Allocation cap into ``resources.capabilities``.

The env config is authoritative for connections; the capabilities row is advertisory
(insert-if-absent, refreshed only by re-registration). Later issues must not read
connection config from the row without an explicit upsert design.

PCIe enumeration and ``list_owned`` reaping are deferred to the provisioning issue,
which creates the domains they would inspect.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kdive.domain.discovery import ResourceRecord
from kdive.domain.models import ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import ResourceStatus
from kdive.providers.libvirt_xml import parse_capabilities_arch
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.transport import (
    OpenConnection,
    open_libvirt,
    remote_connection,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env


class RemoteLibvirtDiscovery:
    """The realized discovery port for one remote qemu+tls host."""

    def __init__(
        self,
        *,
        config: RemoteLibvirtConfig,
        secret_backend: SecretBackend,
        open_connection: OpenConnection,
        pki_base_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._secret_backend = secret_backend
        self._open_connection = open_connection
        self._pki_base_dir = pki_base_dir
        self.host_uri = config.uri

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtDiscovery:
        """Build from ``KDIVE_REMOTE_LIBVIRT_*``.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` when the operator config is
                absent or invalid (see :func:`remote_config_from_env`).
        """
        return cls(
            config=remote_config_from_env(),
            secret_backend=secret_backend_from_env(registry=secret_registry),
            open_connection=open_libvirt,
        )

    def list_resources(self) -> list[ResourceRecord]:
        """Return one ``ResourceRecord`` for the remote host (resource id = the URI).

        Raises:
            CategorizedError: ``TRANSPORT_FAILURE`` when the TLS connect fails, or
                ``CONFIGURATION_ERROR`` for unresolvable cert refs or an unsafe URI.
        """
        with remote_connection(
            self._config,
            self._secret_backend,
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        ) as conn:
            info = conn.getInfo()
            arch = parse_capabilities_arch(conn.getCapabilities())
        refs = self._config.cert_refs
        capabilities: dict[str, Any] = {
            "arch": arch,
            "vcpus": int(info[2]),
            "memory_mb": int(info[1]),
            "transports": ["gdbstub"],
            "connect_uri": self._config.uri,
            "tls_client_cert_ref": refs.client_cert_ref,
            "tls_client_key_ref": refs.client_key_ref,
            "tls_ca_cert_ref": refs.ca_cert_ref,
            CONCURRENT_ALLOCATION_CAP_KEY: self._config.concurrent_allocation_cap,
            # Provisioning host topology (ADR-0080 §5); advisory, like the rest of
            # the row — the env config stays authoritative for ops.
            "storage_pool": self._config.storage_pool,
            "gdbstub_port_min": self._config.gdb_port_min,
            "gdbstub_port_max": self._config.gdb_port_max,
        }
        if self._config.gdb_addr is not None:
            capabilities["gdbstub_addr"] = self._config.gdb_addr
        return [
            ResourceRecord(
                resource_id=self.host_uri,
                kind=ResourceKind.REMOTE_LIBVIRT,
                capabilities=capabilities,
                status=ResourceStatus.AVAILABLE,
            )
        ]
