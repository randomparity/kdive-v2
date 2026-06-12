"""Local-libvirt provisioning XML rendering."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from uuid import UUID

from kdive.profiles.provisioning import ProvisioningProfile, require_concrete_sizing
from kdive.profiles.provisioning import validate_profile as _validate_profile
from kdive.providers.libvirt_xml import KDIVE_METADATA_NS, register_kdive_namespace
from kdive.providers.runtime_paths import console_log_path, domain_name_for

_DEFAULT_MACHINE = "q35"


def _ensure_kdive_namespace_registered() -> None:
    """Register the kdive XML prefix when rendering domain XML."""
    # ElementTree keeps namespace prefixes in process-global state. Keep that mutation out of
    # import time and perform it at the rendering boundary that needs deterministic prefixes.
    register_kdive_namespace()


def render_domain_xml(system_id: UUID, profile: ProvisioningProfile, *, disk_path: str) -> str:
    """Render the tagged libvirt domain XML for a System (ADR-0025 §3)."""
    _ensure_kdive_namespace_registered()
    _validate_profile(profile)
    require_concrete_sizing(profile)
    section = profile.provider.local_libvirt
    machine = section.domain_xml_params.get("machine", _DEFAULT_MACHINE)

    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = domain_name_for(system_id)
    ET.SubElement(domain, "uuid").text = str(system_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(profile.memory_mb)
    ET.SubElement(domain, "vcpu").text = str(profile.vcpu)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=profile.arch, machine=machine).text = "hvm"
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", file=disk_path)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    serial = ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(serial, "log", file=str(console_log_path(system_id)))
    ET.SubElement(serial, "target", port="0")
    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{KDIVE_METADATA_NS}}}system").text = str(system_id)

    return ET.tostring(domain, encoding="unicode")
