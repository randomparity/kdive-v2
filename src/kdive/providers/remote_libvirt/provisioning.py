"""Remote-libvirt Provisioning plane: disk-image base-OS define/start over TLS (ADR-0080).

`RemoteLibvirtProvision` realizes the `Provisioner` port against a remote `qemu+tls://`
host: it renders a gdbstub-enabled domain XML carrying the qemu-guest-agent virtio-serial
channel, creates the per-System qcow2 overlay as a storage-pool volume backed by the
operator-staged base image (no shared filesystem, so no worker-side ``qemu-img``), and
defines+starts the domain over the ADR-0077 mutual-TLS transport.

The **domain definition is the gdbstub port registry**: the per-System port is allocated
by enumerating the ports recorded in the defined ``kdive-`` domains' XML and rendered into
``<qemu:commandline>``, so the record is atomic with ``defineXML``, freed by ``undefine``,
and read over the same TLS connection by the Connect plane (ADR-0079/0080). Domain XML is
*constructed* with ``xml.etree.ElementTree`` (no string interpolation); XML *received from
the host* (domain dumps polled for the agent channel state) is parsed with ``defusedxml``
— it crosses the same trust boundary as discovery's capabilities XML.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    RemoteLibvirtProfile,
    require_concrete_sizing,
)
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.transport import remote_connection
from kdive.providers.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)

# Duplicated from local-libvirt deliberately: no shared libvirt_common layer (ADR-0076).
KDIVE_METADATA_NS = "https://kdive.dev/libvirt/1"
QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"

_DEFAULT_NETWORK = "default"
_DOMAIN_PREFIX = "kdive-"
_GUEST_AGENT_CHANNEL = "org.qemu.guest_agent.0"
# Bounded start-failure port advance (ADR-0080 §2): a squatted port or a define→start
# race is skipped without message sniffing; an unrelated start fault fails fast after
# this many attempts.
_START_ATTEMPTS = 3
_AGENT_TIMEOUT_S = 180.0
_AGENT_POLL_S = 2.0

_namespaces_registered = False


def _ensure_namespaces_registered() -> None:
    """Register XML prefixes at the rendering boundary (process-global ET state)."""
    global _namespaces_registered
    if _namespaces_registered:
        return
    ET.register_namespace("kdive", KDIVE_METADATA_NS)
    ET.register_namespace("qemu", QEMU_NS)
    _namespaces_registered = True


def overlay_volume_name(system_id: UUID | str) -> str:
    """The per-System overlay volume name in the host's storage pool (ADR-0080 §3).

    Accepts the raw id string too, so ``teardown`` can derive it from the domain name
    without a UUID parse (mirrors the local overlay-path contract, ADR-0060).
    """
    return f"kdive-{system_id}-overlay.qcow2"


def render_volume_xml(name: str, *, capacity_bytes: int, backing_path: str) -> str:
    """Render the overlay volume XML: qcow2, backed by the base image volume.

    Capacity is the base volume's virtual capacity — a smaller value would truncate
    the guest's view of the disk (ADR-0080 §3).
    """
    volume = ET.Element("volume")
    ET.SubElement(volume, "name").text = name
    ET.SubElement(volume, "capacity").text = str(capacity_bytes)
    target = ET.SubElement(volume, "target")
    ET.SubElement(target, "format", type="qcow2")
    backing = ET.SubElement(volume, "backingStore")
    ET.SubElement(backing, "path").text = backing_path
    ET.SubElement(backing, "format", type="qcow2")
    return ET.tostring(volume, encoding="unicode")


def render_domain_xml(
    system_id: UUID,
    profile: ProvisioningProfile,
    *,
    pool: str,
    volume: str,
    gdb_addr: str,
    gdb_port: int,
    network: str = _DEFAULT_NETWORK,
    machine: str = "pc",
) -> str:
    """Render the tagged remote domain XML (ADR-0080 §2/§4).

    Renders the domain shell, the overlay disk (``type='volume'``), boot-from-disk, a
    virtio NIC on the host's ``network`` (the in-guest artifact channel pulls presigned
    GETs and pushes the vmcore PUT over it — ADR-0078/0082/0084 depend on guest egress),
    the qemu-guest-agent virtio-serial channel, a pty serial console **without** a
    worker-local ``<log>`` tee (the path would be on the remote host), the kdive
    metadata tag, and the gdbstub QEMU passthrough args — the per-System port record.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a profile without a remote
            section or without concrete sizing.
    """
    _ensure_namespaces_registered()
    if profile.provider.remote_libvirt_section is None:
        raise CategorizedError(
            "provisioning profile has no remote-libvirt provider section",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    require_concrete_sizing(profile)

    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = domain_name_for(system_id)
    # A deterministic uuid (= the System id) makes `defineXML` redefine the System's
    # existing domain in place on a provision retry (ADR-0025/ADR-0080).
    ET.SubElement(domain, "uuid").text = str(system_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(profile.memory_mb)
    ET.SubElement(domain, "vcpu").text = str(profile.vcpu)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=profile.arch, machine=machine).text = "hvm"
    ET.SubElement(os_el, "boot", dev="hd")
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", type="volume", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", pool=pool, volume=volume)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    interface = ET.SubElement(devices, "interface", type="network")
    ET.SubElement(interface, "source", network=network)
    ET.SubElement(interface, "model", type="virtio")
    serial = ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")
    channel = ET.SubElement(devices, "channel", type="unix")
    ET.SubElement(channel, "target", type="virtio", name=_GUEST_AGENT_CHANNEL)
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{KDIVE_METADATA_NS}}}system").text = str(system_id)
    commandline = ET.SubElement(domain, f"{{{QEMU_NS}}}commandline")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value="-gdb")
    ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=f"tcp:{gdb_addr}:{gdb_port}")
    return ET.tostring(domain, encoding="unicode")


def recorded_gdb_port(domain_xml: str) -> int | None:
    """The gdbstub port a domain's XML records, or ``None`` if absent/malformed.

    Parsed with ``defusedxml`` — the XML is emitted by the remote libvirtd. Tolerant
    by design: a domain without the passthrough args (or with mangled ones) simply
    owns no port; allocation treats malformed records as absent.
    """
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except ET.ParseError:
        return None
    args = [
        arg.get("value") for arg in root.findall(f"./{{{QEMU_NS}}}commandline/{{{QEMU_NS}}}arg")
    ]
    for previous, current in zip(args, args[1:], strict=False):
        if previous != "-gdb" or current is None:
            continue
        _, _, port_text = current.rpartition(":")
        try:
            return int(port_text)
        except ValueError:
            return None
    return None


def allocate_gdb_port(
    used: dict[str, int],
    *,
    own_name: str,
    port_min: int,
    port_max: int,
    exclude: set[int] | None = None,
) -> int:
    """Pick the System's gdbstub port from the configured range (ADR-0080 §2).

    Reuses the System's own recorded in-range port (stable across retries); otherwise
    the lowest port not recorded by another defined kdive domain and not in
    ``exclude`` (ports already tried in this attempt's bounded start-failure advance).

    Raises:
        CategorizedError: ``PROVISIONING_FAILURE`` when the range is exhausted.
    """
    own = used.get(own_name)
    if own is not None and port_min <= own <= port_max and (exclude is None or own not in exclude):
        return own
    taken = {port for name, port in used.items() if name != own_name}
    if exclude:
        taken |= exclude
    for port in range(port_min, port_max + 1):
        if port not in taken:
            return port
    raise CategorizedError(
        "gdbstub port range is exhausted on the remote host",
        category=ErrorCategory.PROVISIONING_FAILURE,
        details={"port_min": port_min, "port_max": port_max, "in_use": len(taken)},
    )


class _Volume(Protocol):
    """The storage-volume slice provisioning uses (duck-typed seam)."""

    def path(self) -> str: ...
    def info(self) -> list[int]: ...
    def delete(self, flags: int = 0) -> int: ...


class _Pool(Protocol):
    """The storage-pool slice provisioning uses (duck-typed seam)."""

    def storageVolLookupByName(self, name: str) -> _Volume: ...  # noqa: N802 - binding name
    def createXML(self, xml: str, flags: int = 0) -> _Volume: ...  # noqa: N802 - binding name


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
    def storagePoolLookupByName(self, name: str) -> _Pool: ...  # noqa: N802 - binding name
    def close(self) -> None: ...


type OpenProvisionConnection = Callable[[str], _ProvisionConn]
type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]


def open_libvirt_provision(uri: str) -> _ProvisionConn:
    """The production opener (live-host path; unit tests inject a fake)."""
    # libvirt ships no type stubs; ty infers `virConnect`, which does not structurally
    # match the protocol. Duck-typed at the seam, as in transport.open_libvirt.
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]


def _agent_channel_connected(domain_xml: str) -> bool:
    """Whether the live XML reports the guest-agent channel ``state='connected'``.

    Parsed with ``defusedxml`` (host-emitted XML); a malformed document reads as
    not-connected — the poll keeps waiting and the bounded timeout owns the failure.
    """
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except ET.ParseError:
        return False
    target = root.find(f"./devices/channel/target[@name='{_GUEST_AGENT_CHANNEL}']")
    return target is not None and target.get("state") == "connected"


def _disk_pool(domain_xml: str) -> str | None:
    """The storage pool the domain's disk records, or ``None`` (tolerant parse)."""
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except ET.ParseError:
        return None
    source = root.find("./devices/disk/source")
    if source is None:
        return None
    return source.get("pool")


@dataclass(frozen=True, slots=True)
class _PreparedOverlay:
    name: str
    created: bool


class RemoteLibvirtProvision:
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
            pool = self._lookup_pool(conn, config.storage_pool)
            overlay = self._ensure_overlay(pool, section.base_image_volume, system_id)
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
                self._cleanup_overlay_if_created(pool, overlay)
                raise
            # Agent-gate failures deliberately leave the domain (and its overlay) in
            # place: the running/exited domain is the diagnosable artifact, and a
            # provision retry converges without tearing it down (ADR-0080 §4).
            self._wait_for_agent(conn, domain_name)
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
        overlay_name = overlay_volume_name(domain_name.removeprefix(_DOMAIN_PREFIX))
        with self._connection(config) as conn:
            recorded_pool = self._teardown_domain(conn, domain_name)
            self._delete_volume(conn, recorded_pool or config.storage_pool, overlay_name)

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

    @staticmethod
    def _lookup_pool(conn: _ProvisionConn, pool_name: str) -> _Pool:
        try:
            return conn.storagePoolLookupByName(pool_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
                raise CategorizedError(
                    f"storage pool {pool_name!r} does not exist on the remote host",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"pool": pool_name},
                ) from exc
            raise _infra("looking up storage pool", pool=pool_name) from exc

    def _ensure_overlay(self, pool: _Pool, base_volume: str, system_id: UUID) -> _PreparedOverlay:
        """Create the per-System overlay volume when absent; reuse it when present.

        A present overlay may be held open by a running QEMU, so it is never
        recreated (ADR-0080 §3).
        """
        name = overlay_volume_name(system_id)
        if self._volume_exists(pool, name):
            return _PreparedOverlay(name=name, created=False)
        try:
            base = pool.storageVolLookupByName(base_volume)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                raise CategorizedError(
                    f"base image volume {base_volume!r} is not staged on the remote "
                    "host's storage pool (an operator prerequisite, ADR-0080)",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"base_image_volume": base_volume},
                ) from exc
            raise _infra("looking up base image volume", volume=base_volume) from exc
        try:
            capacity = int(base.info()[1])
            xml = render_volume_xml(name, capacity_bytes=capacity, backing_path=base.path())
            pool.createXML(xml)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "could not create the per-System overlay volume",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"volume": name},
            ) from exc
        return _PreparedOverlay(name=name, created=True)

    @staticmethod
    def _volume_exists(pool: _Pool, name: str) -> bool:
        try:
            pool.storageVolLookupByName(name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return False
            raise _infra("looking up overlay volume", volume=name) from exc
        return True

    def _cleanup_overlay_if_created(self, pool: _Pool, overlay: _PreparedOverlay) -> None:
        """Reclaim an overlay this attempt created; never one a running System owns."""
        if not overlay.created:
            return
        try:
            pool.storageVolLookupByName(overlay.name).delete()
        except libvirt.libvirtError:
            _log.warning("failed to remove overlay volume %s after failed provision", overlay.name)

    @staticmethod
    def _used_gdb_ports(conn: _ProvisionConn) -> dict[str, int]:
        """Ports recorded by defined kdive domains; a domain vanishing mid-walk is skipped."""
        used: dict[str, int] = {}
        try:
            domains = conn.listAllDomains()
        except libvirt.libvirtError as exc:
            raise _infra("listing domains for gdbstub port enumeration") from exc
        for domain in domains:
            try:
                name = domain.name()
                if not name.startswith(_DOMAIN_PREFIX):
                    continue
                port = recorded_gdb_port(domain.XMLDesc())
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    continue  # being torn down concurrently; its port is being released
                raise _infra("enumerating gdbstub ports") from exc
            if port is not None:
                used[name] = port
        return used

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
        used = self._used_gdb_ports(conn)
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

    def _wait_for_agent(self, conn: _ProvisionConn, domain_name: str) -> None:
        """Poll the live XML until the guest-agent channel reports connected.

        Read-only (no agent command — the exec seam is the artifact-channel issue's);
        fails fast if the domain exits during boot rather than burning the timeout.
        """
        deadline = self._monotonic() + self._agent_timeout_s
        while True:
            try:
                domain = conn.lookupByName(domain_name)
                running = bool(domain.isActive())
                connected = running and _agent_channel_connected(domain.XMLDesc())
            except libvirt.libvirtError as exc:
                raise _infra("polling the guest-agent channel", domain=domain_name) from exc
            if connected:
                return
            if not running:
                raise CategorizedError(
                    "domain exited during boot before the guest agent connected",
                    category=ErrorCategory.PROVISIONING_FAILURE,
                    details={"domain": domain_name},
                )
            if self._monotonic() >= deadline:
                raise CategorizedError(
                    f"guest agent did not connect within {self._agent_timeout_s:g}s",
                    category=ErrorCategory.PROVISIONING_FAILURE,
                    details={"domain": domain_name, "timeout_s": self._agent_timeout_s},
                )
            self._sleep(self._agent_poll_s)

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

    @staticmethod
    def _delete_volume(conn: _ProvisionConn, pool_name: str, volume_name: str) -> None:
        try:
            pool = conn.storagePoolLookupByName(pool_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
                return  # the pool is gone, and the volume with it
            raise _infra("looking up storage pool", pool=pool_name) from exc
        try:
            volume = pool.storageVolLookupByName(volume_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return  # already gone: the achieved post-state
            raise _infra("looking up overlay volume", volume=volume_name) from exc
        try:
            volume.delete()
        except libvirt.libvirtError as exc:
            raise _infra("deleting overlay volume", volume=volume_name) from exc


def _infra(verb: str, **details: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=dict(details),
    )
