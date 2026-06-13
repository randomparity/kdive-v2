"""Ephemeral remote-libvirt build VM: provision a throwaway builder, exec, tear down (ADR-0100).

`EphemeralBuildVm.session` provisions a ``kdive-build-<run_id>`` domain on the configured
remote-libvirt host (a qcow2 overlay over the operator-staged base build image, the
guest-agent channel, generous vCPU/RAM, and **no gdbstub** — a builder is not a debug target),
waits for its guest agent, yields a :class:`GuestExecBuildTransport` bound to the domain, and
tears the domain + overlay down in a ``finally``. The reconciler reaps a leaked builder by
domain marker + owning-BUILD-job liveness (see the ``build_vm_reaper`` module).

The build domain name (``kdive-build-<run_id>``) and overlay name (``kdive-build-<run_id>.qcow2``)
are disjoint from the per-System schemes, and the domain records no gdbstub port, so it is
inert for System gdbstub-port enumeration (ADR-0100). The blocking libvirt calls run only
under the ``live_vm`` gate; orchestration is unit-tested with an injected fake connection.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.build_host.guest_exec_transport import GuestExecBuildTransport
from kdive.providers.libvirt_xml import KDIVE_METADATA_NS, register_kdive_namespace
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.guest.agent import AgentCommand, qemu_agent_command
from kdive.providers.remote_libvirt.lifecycle.provisioning import (
    OpenProvisionConnection,
    open_libvirt_provision,
)
from kdive.providers.remote_libvirt.lifecycle.readiness import Monotonic, Sleep, wait_for_agent
from kdive.providers.remote_libvirt.lifecycle.storage import (
    delete_volume,
    ensure_named_overlay,
    lookup_pool,
)
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

__all__ = [
    "BUILD_DOMAIN_PREFIX",
    "EphemeralBuildVm",
    "build_domain_name",
    "build_overlay_volume_name",
    "ephemeral_build_session",
    "render_build_domain_xml",
]

_log = logging.getLogger(__name__)

BUILD_DOMAIN_PREFIX = "kdive-build-"
_GUEST_AGENT_CHANNEL = "org.qemu.guest_agent.0"

# Fixed build-VM sizing: a kernel compile wants several cores and headroom. Tunable via a
# follow-up if an operator's host topology needs it (no speculative env knob today).
_BUILD_VCPUS = 4
_BUILD_MEMORY_MIB = 8192
_BUILD_ARCH = "x86_64"

_AGENT_TIMEOUT_S = 180.0
_AGENT_POLL_S = 2.0


def build_domain_name(run_id: UUID) -> str:
    """The ephemeral build VM's domain name (the reaper marker), disjoint from System names."""
    return f"{BUILD_DOMAIN_PREFIX}{run_id}"


def build_overlay_volume_name(run_id: UUID) -> str:
    """The build VM's overlay volume name, disjoint from the per-System overlay scheme."""
    return f"{BUILD_DOMAIN_PREFIX}{run_id}.qcow2"


def render_build_domain_xml(
    run_id: UUID,
    *,
    pool: str,
    volume: str,
    network: str,
    machine: str,
    vcpus: int = _BUILD_VCPUS,
    memory_mib: int = _BUILD_MEMORY_MIB,
    arch: str = _BUILD_ARCH,
) -> str:
    """Render the build VM's domain XML: agent channel + overlay disk + network, no gdbstub.

    Unlike the System domain (ADR-0080), this records no ``<qemu:commandline>`` gdbstub args —
    a builder is not a debug target — so it is inert for ``used_gdb_ports`` enumeration.
    """
    register_kdive_namespace()
    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = build_domain_name(run_id)
    ET.SubElement(domain, "uuid").text = str(run_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(memory_mib)
    ET.SubElement(domain, "vcpu").text = str(vcpus)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=arch, machine=machine).text = "hvm"
    ET.SubElement(os_el, "boot", dev="hd")
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", type="volume", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", pool=pool, volume=volume)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    interface = ET.SubElement(devices, "interface", type="network")
    ET.SubElement(interface, "source", network=network)
    ET.SubElement(interface, "model", type="virtio")
    channel = ET.SubElement(devices, "channel", type="unix")
    ET.SubElement(channel, "target", type="virtio", name=_GUEST_AGENT_CHANNEL)
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{KDIVE_METADATA_NS}}}build").text = str(run_id)
    return ET.tostring(domain, encoding="unicode")


class _Domain(Protocol):
    def name(self) -> str: ...
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...


class _BuildConn(Protocol):
    def defineXML(self, xml: str) -> _Domain: ...  # noqa: N802 - binding name
    def lookupByName(self, name: str) -> Any: ...  # noqa: N802 - binding name
    def storagePoolLookupByName(self, name: str) -> Any: ...  # noqa: N802 - binding name
    def close(self) -> None: ...


class EphemeralBuildVm:
    """Provision/teardown a throwaway remote-libvirt build VM (ADR-0100).

    Buildable without operator config (ADR-0076): the ``KDIVE_REMOTE_LIBVIRT_*`` config is read
    per op via ``config_factory``. All slow seams (connection opener, agent command, clock,
    sleep) are injected; unit tests never touch a real host.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = remote_config_from_env,
        open_connection: OpenProvisionConnection = open_libvirt_provision,
        agent_command: AgentCommand = qemu_agent_command,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
        agent_timeout_s: float = _AGENT_TIMEOUT_S,
        agent_poll_s: float = _AGENT_POLL_S,
    ) -> None:
        self._secret_registry = secret_registry
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._agent_command = agent_command
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir
        self._sleep = sleep
        self._monotonic = monotonic
        self._agent_timeout_s = agent_timeout_s
        self._agent_poll_s = agent_poll_s

    @contextmanager
    def session(self, base_image_volume: str, *, run_id: UUID) -> Iterator[GuestExecBuildTransport]:
        """Provision the build VM, yield a transport bound to it, tear it down on exit.

        Args:
            base_image_volume: The operator-staged base build-image volume to overlay.
            run_id: The owning Run; names the domain/overlay and is the reaper marker.

        Yields:
            A :class:`GuestExecBuildTransport` bound to the live build VM's domain.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for missing operator config / absent
                pool/base volume; ``PROVISIONING_FAILURE`` for overlay/define/start or an agent
                that never connects; ``TRANSPORT_FAILURE`` when the TLS connect fails.
        """
        config = self._config_factory()
        domain_name = build_domain_name(run_id)
        with self._connection(config) as conn:
            pool = lookup_pool(conn, config.storage_pool)
            ensure_named_overlay(pool, base_image_volume, build_overlay_volume_name(run_id))
            try:
                self._define_and_start(conn, run_id, config=config)
                wait_for_agent(
                    conn,
                    domain_name,
                    monotonic=self._monotonic,
                    sleep=self._sleep,
                    timeout_s=self._agent_timeout_s,
                    poll_s=self._agent_poll_s,
                )
                transport = GuestExecBuildTransport(
                    domain=conn.lookupByName(domain_name),
                    agent_command=self._agent_command,
                    secret_registry=self._secret_registry,
                )
                yield transport
            finally:
                self._teardown(conn, run_id, config)

    def _connection(self, config: RemoteLibvirtConfig) -> Any:
        return remote_connection(
            config,
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )

    def _define_and_start(
        self, conn: _BuildConn, run_id: UUID, *, config: RemoteLibvirtConfig
    ) -> None:
        """Define+start the build domain; an already-running domain is the achieved post-state."""
        xml = render_build_domain_xml(
            run_id,
            pool=config.storage_pool,
            volume=build_overlay_volume_name(run_id),
            network=config.network,
            machine=config.machine,
        )
        try:
            domain = conn.defineXML(xml)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "libvirt failed to define the build VM",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"run_id": str(run_id)},
            ) from exc
        try:
            domain.create()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                return  # already running: the achieved post-state
            raise CategorizedError(
                "libvirt failed to start the build VM",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"run_id": str(run_id)},
            ) from exc

    def _teardown(self, conn: _BuildConn, run_id: UUID, config: RemoteLibvirtConfig) -> None:
        """Destroy+undefine the build domain and delete its overlay; best-effort (reaper backstops).

        Absent domain / not-running / absent volume are achieved post-states. Teardown never
        raises — a failure leaves a leak the reconciler reaps by job liveness.
        """
        domain_name = build_domain_name(run_id)
        try:
            domain = conn.lookupByName(domain_name)
            try:
                domain.destroy()
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                    raise
            domain.undefine()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                _log.warning("build VM %s domain teardown failed; reaper reclaims", domain_name)
        try:
            delete_volume(conn, config.storage_pool, build_overlay_volume_name(run_id))
        except CategorizedError:
            _log.warning("build VM %s overlay delete failed; reaper reclaims", domain_name)


@contextmanager
def ephemeral_build_session(
    base_image_volume: str, secret_registry: SecretRegistry, *, run_id: UUID
) -> Iterator[GuestExecBuildTransport]:
    """Module-level seam: build a default :class:`EphemeralBuildVm` and run its session.

    The BUILD handler imports this so a test can substitute a fake session without a libvirt
    host; production delegates to a default-seam :class:`EphemeralBuildVm`.
    """
    vm = EphemeralBuildVm(secret_registry=secret_registry)
    with vm.session(base_image_volume, run_id=run_id) as transport:
        yield transport
