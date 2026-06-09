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
import xml.etree.ElementTree as ET
from uuid import UUID

from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile, require_concrete_sizing
from kdive.providers.runtime_paths import domain_name_for

_log = logging.getLogger(__name__)

# Duplicated from local-libvirt deliberately: no shared libvirt_common layer (ADR-0076).
KDIVE_METADATA_NS = "https://kdive.dev/libvirt/1"
QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"

_DEFAULT_MACHINE = "q35"
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
) -> str:
    """Render the tagged remote domain XML (ADR-0080 §2/§4).

    Renders the domain shell, the overlay disk (``type='volume'``), boot-from-disk, the
    qemu-guest-agent virtio-serial channel, a pty serial console **without** a
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
    ET.SubElement(os_el, "type", arch=profile.arch, machine=_DEFAULT_MACHINE).text = "hvm"
    ET.SubElement(os_el, "boot", dev="hd")
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", type="volume", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", pool=pool, volume=volume)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
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
    if own is not None and port_min <= own <= port_max:
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
