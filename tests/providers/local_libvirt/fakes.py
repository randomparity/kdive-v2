"""Reusable local-libvirt fakes for provider and MCP tests.

`FakeLibvirtConn` returns canned host info / capabilities XML / domains so discovery is
covered without a real libvirt host (no `live_vm`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import libvirt

_CAPS_XML = "<capabilities><host><cpu><arch>x86_64</arch></cpu></host></capabilities>"


def libvirt_error(code: int) -> libvirt.libvirtError:
    """Build a libvirtError whose get_error_code() returns ``code``."""
    err = libvirt.libvirtError("synthetic")
    # get_error_code() reads self.err[0]; libvirtError leaves err=None with no live error.
    err.err = (code, 0, "synthetic", 0, "", None, None, 0, 0)
    return err


@dataclass
class FakeDomain:
    domain_name: str
    system_id: str | None  # None → no kdive metadata (raises VIR_ERR_NO_DOMAIN_METADATA)
    raise_code: int | None = None  # override: raise a libvirtError with this code
    calls: list[str] = field(default_factory=list)  # records control ops in call order
    raise_on: dict[str, int] = field(default_factory=dict)  # op -> libvirt error code to raise
    active: bool = False  # isActive() result; boot's "destroy if running" reads it
    xml_desc: str | None = None  # XMLDesc() result; install reads it to add a direct-kernel <os>

    def name(self) -> str:
        return self.domain_name

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - mirrors the libvirt binding name
        return (
            self.xml_desc
            if self.xml_desc is not None
            else (
                f'<domain type="kvm"><name>{self.domain_name}</name>'
                f'<memory unit="MiB">2048</memory><vcpu>2</vcpu>'
                f'<os><type arch="x86_64" machine="q35">hvm</type></os>'
                f'<devices><disk type="file" device="disk">'
                f'<source file="/var/lib/kdive/rootfs.qcow2"/>'
                f'<target dev="vda" bus="virtio"/></disk></devices></domain>'
            )
        )

    def metadata(self, kind: int, uri: str | None, flags: int) -> str:
        if self.raise_code is not None:
            raise libvirt_error(self.raise_code)
        if self.system_id is None:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN_METADATA)
        return f'<kdive:system xmlns:kdive="{uri}">{self.system_id}</kdive:system>'

    def _maybe_raise(self, op: str) -> None:
        code = self.raise_on.get(op)
        if code is not None:
            raise libvirt_error(code)

    def create(self) -> int:
        self.calls.append("create")
        self._maybe_raise("create")
        return 0

    def destroy(self) -> int:
        self.calls.append("destroy")
        self._maybe_raise("destroy")
        return 0

    def reset(self, flags: int = 0) -> int:
        self.calls.append("reset")
        self._maybe_raise("reset")
        return 0

    def reboot(self, flags: int = 0) -> int:
        self.calls.append("reboot")
        self._maybe_raise("reboot")
        return 0

    def injectNMI(self, flags: int = 0) -> int:  # noqa: N802 - mirrors the libvirt binding name
        self.calls.append("injectNMI")
        self._maybe_raise("injectNMI")
        return 0

    def isActive(self) -> int:  # noqa: N802 - mirrors the libvirt binding name
        return 1 if self.active else 0


@dataclass
class FakeNodeDevice:
    """A libvirt node device exposing name()/XMLDesc(), for PCIe discovery tests."""

    device_name: str
    xml: str

    def name(self) -> str:
        return self.device_name

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - mirrors the libvirt binding name
        return self.xml


def pci_nodedev_xml(
    *,
    name: str = "pci_0000_3b_00_0",
    cls: str = "0x020000",
    domain: int = 0,
    bus: int = 0x3B,
    slot: int = 0,
    function: int = 0,
    vendor_id: str = "0x8086",
    device_id: str = "0x1572",
    product_label: str | None = "Ethernet Controller X710",
) -> str:
    """Render a libvirt nodedev PCI XML; bus/slot/function are DECIMAL like real nodedev."""
    product = (
        f"<product id='{device_id}'>{product_label}</product>"
        if product_label is not None
        else f"<product id='{device_id}'/>"
    )
    return (
        f"<device><name>{name}</name>"
        f"<capability type='pci'>"
        f"<class>{cls}</class>"
        f"<domain>{domain}</domain><bus>{bus}</bus>"
        f"<slot>{slot}</slot><function>{function}</function>"
        f"{product}"
        f"<vendor id='{vendor_id}'>Intel Corporation</vendor>"
        f"</capability></device>"
    )


@dataclass
class FakeLibvirtConn:
    caps_xml: str = _CAPS_XML
    info: list[object] = field(default_factory=lambda: ["x86_64", 16384, 8, 2400, 1, 1, 4, 2])
    domains: list[FakeDomain] = field(default_factory=list)
    node_devices: list[FakeNodeDevice] = field(default_factory=list)  # PCI nodedev inventory
    lookup: dict[str, FakeDomain] = field(default_factory=dict)  # name -> domain for control ops
    defined_xml: list[str] = field(default_factory=list)  # captures defineXML payloads in order
    define_error: int | None = None  # libvirt error code defineXML raises, if set

    def getInfo(self) -> list[object]:
        return self.info

    def getCapabilities(self) -> str:
        return self.caps_xml

    def listAllDevices(self, flags: int = 0) -> list[FakeNodeDevice]:  # noqa: N802
        return self.node_devices

    def listAllDomains(self, flags: int = 0) -> list[FakeDomain]:
        return self.domains

    def lookupByName(self, name: str) -> FakeDomain:
        domain = self.lookup.get(name)
        if domain is None:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return domain

    def defineXML(self, xml: str) -> FakeDomain:  # noqa: N802 - mirrors the libvirt binding name
        self.defined_xml.append(xml)
        if self.define_error is not None:
            raise libvirt_error(self.define_error)
        # Return (and register) a domain keyed on the rendered <name>, so a later
        # lookupByName(domain_name) in boot() finds the just-defined domain.
        return self.lookup.setdefault(
            _name_from_domain_xml(xml), FakeDomain(domain_name="defined", system_id=None)
        )

    def close(self) -> int:
        return 0


def _name_from_domain_xml(xml: str) -> str:
    """Extract the <name> text from a rendered domain XML (test helper)."""
    import xml.etree.ElementTree as ET

    name_el = ET.fromstring(xml).find("name")  # noqa: S314 - trusted, self-rendered test XML
    return name_el.text or "" if name_el is not None else ""
