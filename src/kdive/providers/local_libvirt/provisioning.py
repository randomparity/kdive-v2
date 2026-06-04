"""Local-libvirt Provisioning plane: define/start and destroy/undefine a tagged domain (ADR-0025).

`LocalLibvirtProvisioning` renders a domain XML from a `ProvisioningProfile` (tagged with the
System id in the kdive metadata element discovery reads), `defineXML`+`create`s it on
`provision`, and `destroy`+`undefine`s it idempotently on `teardown`, over an injected
connection factory (unit tests never touch a real host; the real `libvirt.open` adapter is
`live_vm`-only). It owns no Postgres — the `systems.*` handlers drive the state machine.

The domain XML is *constructed* with `xml.etree.ElementTree` (no string interpolation, so a
profile value cannot inject XML; no untrusted-input parse here, so no XXE surface). It renders
the domain shell, the rootfs disk, and the metadata tag — no `<kernel>`/`<cmdline>`: libvirt
ignores `<os><cmdline>` without a `<kernel>` element, and the test kernel plus its
`crashkernel=` kdump reservation are the install/boot plane's (#17).
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.discovery import _KDIVE_METADATA_NS

_URI_ENV = "KDIVE_LIBVIRT_URI"
_DEFAULT_URI = "qemu:///system"
_DEFAULT_MACHINE = "q35"
SUPPORTED_DOMAIN_XML_PARAMS = frozenset({"machine"})


class _LibvirtDomain(Protocol):
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...


class _LibvirtConn(Protocol):
    def defineXML(self, xml: str) -> _LibvirtDomain: ...
    def lookupByName(self, name: str) -> _LibvirtDomain: ...


type Connect = Callable[[], _LibvirtConn]


def domain_name_for(system_id: UUID) -> str:
    """The deterministic libvirt domain name for a System."""
    return f"kdive-{system_id}"


def validate_profile(profile: ProvisioningProfile) -> None:
    """Reject a profile whose libvirt ``domain_xml_params`` carry an unsupported key.

    Called at the tool boundary so a bad param is a synchronous ``configuration_error``
    response, not a dead-lettered provision job.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` naming the unsupported key(s).
    """
    params = profile.provider.local_libvirt.domain_xml_params
    unknown = sorted(set(params) - SUPPORTED_DOMAIN_XML_PARAMS)
    if unknown:
        raise CategorizedError(
            f"unsupported domain_xml_params: {', '.join(unknown)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"unsupported": unknown, "supported": sorted(SUPPORTED_DOMAIN_XML_PARAMS)},
        )


def render_domain_xml(system_id: UUID, profile: ProvisioningProfile) -> str:
    """Render the tagged libvirt domain XML for a System (ADR-0025 §3).

    Renders the domain shell, the rootfs disk, and the kdive metadata tag — no
    ``<kernel>``/``<cmdline>`` (the kdump ``crashkernel=`` reservation is the install/boot
    plane's, #17, and is inert without a ``<kernel>`` element).
    """
    validate_profile(profile)
    section = profile.provider.local_libvirt
    machine = section.domain_xml_params.get("machine", _DEFAULT_MACHINE)

    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = domain_name_for(system_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(profile.memory_mb)
    ET.SubElement(domain, "vcpu").text = str(profile.vcpu)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=profile.arch, machine=machine).text = "hvm"
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "source", file=section.rootfs_image_ref)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{_KDIVE_METADATA_NS}}}system").text = str(system_id)

    ET.register_namespace("kdive", _KDIVE_METADATA_NS)
    return ET.tostring(domain, encoding="unicode")


class LocalLibvirtProvisioning:
    """The `ProvisioningPlane` for the local libvirt host (define/start, destroy/undefine)."""

    def __init__(self, *, connect: Connect) -> None:
        self._connect = connect

    @classmethod
    def from_env(cls) -> LocalLibvirtProvisioning:
        """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect."""
        host_uri = os.environ.get(_URI_ENV, _DEFAULT_URI)
        # `virConnect` structurally satisfies the narrow `_LibvirtConn` Protocol (only
        # `defineXML`/`lookupByName`), so no suppression is needed at this seam.
        return cls(connect=lambda: libvirt.open(host_uri))

    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str:
        """Define and start the tagged domain; return its name.

        Raises:
            CategorizedError: ``PROVISIONING_FAILURE`` on any libvirt error.
        """
        xml = render_domain_xml(system_id, profile)
        try:
            domain = self._connect().defineXML(xml)
            domain.create()
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "libvirt failed to define/start the domain",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"system_id": str(system_id)},
            ) from exc
        return domain_name_for(system_id)

    def teardown(self, domain_name: str) -> None:
        """Destroy and undefine the domain; idempotent over an already-absent domain.

        "No such domain" on lookup/undefine and "not running" on destroy are the achieved
        post-state, so they are swallowed; any other libvirt error fails.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` on any other libvirt error.
        """
        conn = self._connect()
        try:
            domain = conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return
            raise self._infra("looking up", domain_name) from exc
        try:
            domain.destroy()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                raise self._infra("destroying", domain_name) from exc
        try:
            domain.undefine()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                raise self._infra("undefining", domain_name) from exc

    @staticmethod
    def _infra(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"domain": domain_name},
        )
