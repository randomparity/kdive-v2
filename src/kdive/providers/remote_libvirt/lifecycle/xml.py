"""Remote-libvirt provisioning XML rendering and tolerant host-XML parsing."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from uuid import UUID

from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile, require_concrete_sizing
from kdive.providers.libvirt_xml import (
    KDIVE_METADATA_NS,
    QEMU_NS,
    register_kdive_namespace,
    register_qemu_namespace,
)
from kdive.providers.runtime_paths import domain_name_for

_DEFAULT_NETWORK = "default"
_GUEST_AGENT_CHANNEL = "org.qemu.guest_agent.0"


def _ensure_namespaces_registered() -> None:
    """Register XML prefixes at the rendering boundary."""
    register_kdive_namespace()
    register_qemu_namespace()


def overlay_volume_name(system_id: UUID | str) -> str:
    """The per-System overlay volume name in the host's storage pool (ADR-0080 §3)."""
    return f"kdive-{system_id}-overlay.qcow2"


def render_volume_xml(name: str, *, capacity_bytes: int, backing_path: str) -> str:
    """Render the overlay volume XML: qcow2, backed by the base image volume."""
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
    """Render the tagged remote domain XML (ADR-0080 §2/§4)."""
    _ensure_namespaces_registered()
    if profile.provider.remote_libvirt_section is None:
        raise CategorizedError(
            "provisioning profile has no remote-libvirt provider section",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    require_concrete_sizing(profile)

    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = domain_name_for(system_id)
    ET.SubElement(domain, "uuid").text = str(system_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(profile.memory_mb)
    ET.SubElement(domain, "vcpu").text = str(profile.vcpu)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=profile.arch, machine=machine).text = "hvm"
    ET.SubElement(os_el, "boot", dev="hd")
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "vmcoreinfo", state="on")
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
    """The gdbstub port a domain's XML records, or ``None`` if absent/malformed."""
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


def agent_channel_connected(domain_xml: str) -> bool:
    """Whether the live XML reports the guest-agent channel ``state='connected'``."""
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except ET.ParseError:
        return False
    target = root.find(f"./devices/channel/target[@name='{_GUEST_AGENT_CHANNEL}']")
    return target is not None and target.get("state") == "connected"


def disk_pool(domain_xml: str) -> str | None:
    """The storage pool the domain's disk records, or ``None``."""
    try:
        root: ET.Element = _safe_fromstring(domain_xml)
    except ET.ParseError:
        return None
    source = root.find("./devices/disk/source")
    if source is None:
        return None
    return source.get("pool")
