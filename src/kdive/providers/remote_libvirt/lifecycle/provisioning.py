"""Remote-libvirt Provisioning plane: disk-image base-OS define/start over TLS (ADR-0080).

`RemoteLibvirtProvisioning` realizes the `Provisioner` port against a remote `qemu+tls://`
host: it renders a gdbstub-enabled domain XML carrying the qemu-guest-agent virtio-serial
channel, creates the per-System qcow2 overlay as a storage-pool volume backed by the
operator-staged base image (no shared filesystem, so no worker-side ``qemu-img``), and
defines+starts the domain over the ADR-0077 mutual-TLS transport.

The **domain definition is the gdbstub port registry**: the per-System port is allocated
by enumerating the ports recorded in the defined ``kdive-`` domains' XML and rendered into
``<qemu:commandline>``, so the record is atomic with ``defineXML``, freed by ``undefine``,
and read over the same TLS connection by the Connect plane (ADR-0079/0080). XML rendering,
gdbstub port enumeration, overlay volume lifecycle, and guest-agent readiness polling live in
focused provider-local collaborators; this facade owns remote config, connection scope, and
define/start/teardown orchestration.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    RemoteLibvirtProfile,
    require_concrete_sizing,
)
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.lifecycle.gdb import (
    DOMAIN_PREFIX,
    allocate_gdb_port,
    used_gdb_ports,
)
from kdive.providers.remote_libvirt.lifecycle.readiness import Monotonic, Sleep, wait_for_agent
from kdive.providers.remote_libvirt.lifecycle.storage import (
    Pool,
    cleanup_overlay_if_created,
    delete_volume,
    ensure_overlay,
    lookup_pool,
)
from kdive.providers.remote_libvirt.lifecycle.xml import (
    KDIVE_METADATA_NS,
    QEMU_NS,
    overlay_volume_name,
    recorded_gdb_port,
    render_domain_xml,
    render_volume_xml,
)
from kdive.providers.remote_libvirt.lifecycle.xml import (
    disk_pool as _disk_pool,
)
from kdive.providers.remote_libvirt.transport import open_libvirt_protocol, remote_connection
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

__all__ = [
    "KDIVE_METADATA_NS",
    "QEMU_NS",
    "RemoteLibvirtProvisioning",
    "allocate_gdb_port",
    "overlay_volume_name",
    "recorded_gdb_port",
    "render_domain_xml",
    "render_volume_xml",
]

_log = logging.getLogger(__name__)

# Bounded start-failure port advance (ADR-0080 §2): a squatted port or a define→start
# race is skipped without message sniffing; an unrelated start fault fails fast after
# this many attempts.
_START_ATTEMPTS = 3
_AGENT_TIMEOUT_S = 180.0
_AGENT_POLL_S = 2.0


class _Domain(Protocol):
    """The domain slice provisioning uses (duck-typed seam)."""

    def name(self) -> str: ...
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...
    def isActive(self) -> int: ...  # noqa: N802 - binding name
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802 - binding name


class _ProvisionConn(Protocol):
    """The connection slice provisioning uses (duck-typed seam)."""

    def defineXML(self, xml: str) -> _Domain: ...  # noqa: N802 - binding name
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - binding name
    def listAllDomains(self, flags: int = 0) -> list[_Domain]: ...  # noqa: N802 - binding name
    def storagePoolLookupByName(self, name: str) -> Pool: ...  # noqa: N802 - binding name
    def close(self) -> None: ...


type OpenProvisionConnection = Callable[[str], _ProvisionConn]


def open_libvirt_provision(uri: str) -> _ProvisionConn:
    """The production opener (live-host path; unit tests inject a fake)."""
    return open_libvirt_protocol(uri)


class RemoteLibvirtProvisioning:
    """The realized Provisioner port for a remote qemu+tls host (ADR-0080).

    Buildable without operator config (ADR-0076): the ``KDIVE_REMOTE_LIBVIRT_*``
    config is read per op via ``config_factory``, never at construction. All slow
    seams (connection opener, clock, sleep) are injected; unit tests never touch a
    real host.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenProvisionConnection = open_libvirt_provision,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
        agent_timeout_s: float = _AGENT_TIMEOUT_S,
        agent_poll_s: float = _AGENT_POLL_S,
    ) -> None:
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir
        self._sleep = sleep
        self._monotonic = monotonic
        self._agent_timeout_s = agent_timeout_s
        self._agent_poll_s = agent_poll_s

    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        """Define and start the System's disk-image domain; wait for its guest agent.

        Idempotent (ADR-0080 §4): a deterministic name+uuid redefines in place on
        retry, ``create()`` treats already-running as the achieved post-state, the
        overlay is created only when absent, and a retry reuses the System's own
        recorded gdbstub port.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a profile without a remote
                section, missing operator config (incl. the gdbstub listen address),
                or an absent pool/base volume; ``PROVISIONING_FAILURE`` for overlay
                creation, define/start, gdbstub-port exhaustion, an agent that never
                connects, or a domain that exits during boot;
                ``INFRASTRUCTURE_FAILURE`` for other provider control-plane faults;
                ``TRANSPORT_FAILURE`` when the TLS connect fails.
        """
        section = self._remote_section(profile)
        require_concrete_sizing(profile)
        config = self._config_factory()
        gdb_addr = config.gdb_addr
        if gdb_addr is None:
            raise CategorizedError(
                "KDIVE_REMOTE_LIBVIRT_GDB_ADDR is not set; the gdbstub listen address "
                "is the ACL'd security boundary and must be named explicitly (ADR-0080)",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        domain_name = domain_name_for(system_id)
        with self._connection(config) as conn:
            pool = lookup_pool(conn, config.storage_pool)
            overlay = ensure_overlay(pool, section.base_image_volume, system_id)
            try:
                self._define_and_start(
                    conn,
                    system_id,
                    profile,
                    config=config,
                    gdb_addr=gdb_addr,
                    overlay_name=overlay.name,
                )
            except CategorizedError:
                cleanup_overlay_if_created(pool, overlay)
                raise
            # Agent-gate failures deliberately leave the domain (and its overlay) in
            # place: the running/exited domain is the diagnosable artifact, and a
            # provision retry converges without tearing it down (ADR-0080 §4).
            wait_for_agent(
                conn,
                domain_name,
                monotonic=self._monotonic,
                sleep=self._sleep,
                timeout_s=self._agent_timeout_s,
                poll_s=self._agent_poll_s,
            )
        return domain_name

    def reprovision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        """Wipe the System's domain + overlay and provision the new profile in place.

        Raises:
            CategorizedError: as :meth:`teardown` and :meth:`provision`.
        """
        self.teardown(domain_name_for(system_id))
        return self.provision(system_id, profile)

    def teardown(self, domain_name: str) -> None:
        """Destroy+undefine the domain and delete its overlay volume; idempotent.

        The overlay's pool is read from the domain XML while the domain exists (the
        record travels with the domain), falling back to the configured pool when it
        is already gone — pool-config drift cannot silently strand the overlay
        (ADR-0080 §4). Absent domain/volume/pool are achieved post-states.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` on any libvirt error other
                than the achieved post-states; ``CONFIGURATION_ERROR`` for missing
                operator config; ``TRANSPORT_FAILURE`` when the TLS connect fails.
        """
        config = self._config_factory()
        overlay_name = overlay_volume_name(domain_name.removeprefix(DOMAIN_PREFIX))
        with self._connection(config) as conn:
            recorded_pool = self._teardown_domain(conn, domain_name)
            delete_volume(conn, recorded_pool or config.storage_pool, overlay_name)

    def _connection(self, config: RemoteLibvirtConfig):  # noqa: ANN202 - contextmanager passthrough
        return remote_connection(
            config,
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )

    @staticmethod
    def _remote_section(profile: ProvisioningProfile) -> RemoteLibvirtProfile:
        section = profile.provider.remote_libvirt_section
        if section is None:
            raise CategorizedError(
                "provisioning profile has no remote-libvirt provider section",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return section

    def _define_and_start(
        self,
        conn: _ProvisionConn,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        config: RemoteLibvirtConfig,
        gdb_addr: str,
        overlay_name: str,
    ) -> None:
        """Define+start with a bounded port advance on start failure (ADR-0080 §2).

        A start failure undefines the just-defined domain (transactional) and retries
        with the next free candidate port — unconditionally on the failure's cause,
        since libvirt does not surface bind-vs-other distinctly; an unrelated fault
        fails the same way again and the bounded retry stops.
        """
        domain_name = domain_name_for(system_id)
        used = used_gdb_ports(conn)
        tried: set[int] = set()
        last_error: libvirt.libvirtError | None = None
        for _attempt in range(_START_ATTEMPTS):
            port = allocate_gdb_port(
                used,
                own_name=domain_name,
                port_min=config.gdb_port_min,
                port_max=config.gdb_port_max,
                exclude=tried,
            )
            xml = render_domain_xml(
                system_id,
                profile,
                pool=config.storage_pool,
                volume=overlay_name,
                gdb_addr=gdb_addr,
                gdb_port=port,
                network=config.network,
                machine=config.machine,
            )
            try:
                domain = conn.defineXML(xml)
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    "libvirt failed to define the domain",
                    category=ErrorCategory.PROVISIONING_FAILURE,
                    details={"system_id": str(system_id)},
                ) from exc
            try:
                domain.create()
                return
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                    return  # already running: the achieved post-state
                try:
                    domain.undefine()
                except libvirt.libvirtError:
                    _log.warning(
                        "failed to undefine domain after a failed start; continuing",
                        exc_info=True,
                    )
                tried.add(port)
                last_error = exc
        raise CategorizedError(
            f"libvirt failed to start the domain after {_START_ATTEMPTS} attempts",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"system_id": str(system_id), "attempts": _START_ATTEMPTS},
        ) from last_error

    def _teardown_domain(self, conn: _ProvisionConn, domain_name: str) -> str | None:
        """Destroy+undefine; return the pool the domain's disk recorded, if readable.

        "No such domain" on lookup/undefine and "not running" on destroy are achieved
        post-states (the local-libvirt error-code contract, duplicated deliberately).
        """
        try:
            domain = conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            raise _infra("looking up", domain=domain_name) from exc
        try:
            recorded_pool = _disk_pool(domain.XMLDesc())
        except libvirt.libvirtError:
            recorded_pool = None
        try:
            domain.destroy()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                raise _infra("destroying", domain=domain_name) from exc
        try:
            domain.undefine()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                raise _infra("undefining", domain=domain_name) from exc
        return recorded_pool


def _infra(verb: str, **details: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=dict(details),
    )
