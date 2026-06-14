"""Local-libvirt Discovery plane (ADR-0023).

`LocalLibvirtDiscovery` enumerates the local libvirt host over an **injected**
connection factory (so unit tests never touch a real host; the real `libvirt.open`
adapter is `live_vm`-only) and advertises arch/cpu/memory, a `gdbstub` transport, and
the per-host concurrent-Allocation cap.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import libvirt
from defusedxml.ElementTree import fromstring as _safe_fromstring

import kdive.config as config
from kdive.domain.discovery import ResourceRecord
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.domain.pcie import PCIE_DEVICES_KEY, PCIeDescriptor
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import ResourceStatus
from kdive.providers.libvirt_xml import (
    KDIVE_METADATA_NS,
    parse_capabilities_arch,
    parse_metadata_system_id,
)
from kdive.providers.local_libvirt.settings import LIBVIRT_ALLOCATION_CAP, LIBVIRT_URI
from kdive.providers.ports import OwnedInfra
from kdive.providers.runtime_paths import system_id_from_domain_name

_log = logging.getLogger(__name__)


class _LibvirtDomain(Protocol):
    def name(self) -> str: ...
    def metadata(self, kind: int, uri: str | None, flags: int) -> str: ...


class _LibvirtNodeDevice(Protocol):
    def name(self) -> str: ...
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802 - libvirt binding name


class _LibvirtConn(Protocol):
    def getInfo(self) -> list[Any]: ...
    def getCapabilities(self) -> str: ...
    def listAllDevices(self, flags: int = 0) -> Sequence[_LibvirtNodeDevice]: ...
    def listAllDomains(self, flags: int = 0) -> Sequence[_LibvirtDomain]: ...


type Connect = Callable[[], _LibvirtConn]


def _hex_id(raw: str) -> str:
    """Normalize a libvirt ``id='0xVVVV'`` attribute to a bare lowercase 4-hex string."""
    return raw.removeprefix("0x").lower()


def _parse_pci_descriptor(device_xml: str) -> PCIeDescriptor | None:
    """Parse one nodedev PCI XML into a static :class:`PCIeDescriptor`; ``None`` if not PCI.

    The nodedev ``<bus>/<slot>/<function>`` fields are **decimal** integers, so the BDF is
    composed with explicit hex formatting (``<slot>31</slot>`` → ``1f``). ``class_code`` is
    the full 6-hex form the matcher prefix-slices; ``label`` is the ``<product>`` text,
    falling back to ``vendor:device`` when the element is self-closing/empty.

    Parsed with ``defusedxml`` (the XML crosses the libvirtd trust boundary, as
    :func:`_parse_arch`): a non-PCI or structurally-incomplete document returns ``None`` so
    one bad device never blanks the inventory; an *attack* document raises (fail loud).
    """
    root: ET.Element = _safe_fromstring(device_xml)
    cap = root.find("./capability[@type='pci']")
    if cap is None:
        return None
    try:
        bdf = _compose_bdf(cap)
        vendor_id = _hex_id(_required_attr(cap, "vendor", "id"))
        device_id = _hex_id(_required_attr(cap, "product", "id"))
        class_code = (cap.findtext("class") or "").removeprefix("0x").lower()
    except (KeyError, TypeError, ValueError):
        return None
    if not class_code:
        return None
    label = (cap.findtext("product") or "").strip() or f"{vendor_id}:{device_id}"
    return PCIeDescriptor(
        bdf=bdf,
        vendor_id=vendor_id,
        device_id=device_id,
        class_code=class_code,
        label=label,
    )


def _required_attr(cap: ET.Element, tag: str, attr: str) -> str:
    """Return ``cap/<tag>``'s ``attr``; raise ``KeyError`` if the element/attribute is absent."""
    element = cap.find(tag)
    if element is None:
        raise KeyError(tag)
    value = element.get(attr)
    if value is None:
        raise KeyError(f"{tag}@{attr}")
    return value


def _compose_bdf(cap: ET.Element) -> str:
    """Compose the canonical hex ``DDDD:BB:SS.F`` BDF from decimal nodedev address fields."""
    domain = int(cap.findtext("domain", default="0"))
    bus = int(cap.findtext("bus", default="0"))
    slot = int(cap.findtext("slot", default="0"))
    function = int(cap.findtext("function", default="0"))
    return f"{domain:04x}:{bus:02x}:{slot:02x}.{function:x}"


class LocalLibvirtDiscovery:
    """The realized discovery port for the local libvirt host."""

    def __init__(self, *, host_uri: str, connect: Connect, concurrent_allocation_cap: int) -> None:
        self.host_uri = host_uri
        self._connect = connect
        self.concurrent_allocation_cap = concurrent_allocation_cap

    @classmethod
    def from_env(cls) -> LocalLibvirtDiscovery:
        """Build from ``KDIVE_LIBVIRT_URI`` + ``KDIVE_LIBVIRT_ALLOCATION_CAP`` (default 1).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the cap env var is not an int.
        """
        host_uri = config.require(LIBVIRT_URI)
        raw_cap = config.require(LIBVIRT_ALLOCATION_CAP)
        try:
            cap = int(raw_cap)
        except ValueError:
            raise CategorizedError(
                f"{LIBVIRT_ALLOCATION_CAP.name}={raw_cap!r} is not an integer",
                category=ErrorCategory.CONFIGURATION_ERROR,
            ) from None
        # libvirt ships no type stubs; ty infers `virConnect` from its source, which does
        # not structurally match `_LibvirtConn` (invariant return types on the binding's
        # list-returning methods). The connection is duck-typed at the seam — scoped ignore.
        return cls(
            host_uri=host_uri,
            connect=lambda: libvirt.open(host_uri),  # ty: ignore[invalid-argument-type]
            concurrent_allocation_cap=cap,
        )

    def list_resources(self) -> list[ResourceRecord]:
        """Return one `ResourceRecord` for the host (discovery-time id = ``host_uri``)."""
        conn = self._connect()
        info = conn.getInfo()
        capabilities: dict[str, Any] = {
            "arch": parse_capabilities_arch(conn.getCapabilities()),
            "vcpus": int(info[2]),
            "memory_mb": int(info[1]),
            "transports": ["gdbstub"],
            CONCURRENT_ALLOCATION_CAP_KEY: self.concurrent_allocation_cap,
            PCIE_DEVICES_KEY: self._list_pcie_descriptors(conn),
        }
        return [
            ResourceRecord(
                resource_id=self.host_uri,
                kind=ResourceKind.LOCAL_LIBVIRT,
                capabilities=capabilities,
                status=ResourceStatus.AVAILABLE,
            )
        ]

    def _list_pcie_descriptors(self, conn: _LibvirtConn) -> list[PCIeDescriptor]:
        """Enumerate the host's PCI node devices into static descriptors (ADR-0068).

        Each malformed/incomplete device is skipped so one bad device never blanks the
        inventory; the descriptor is static (no occupancy flag), so a re-scan is idempotent
        against live claims.
        """
        descriptors: list[PCIeDescriptor] = []
        for device in conn.listAllDevices(libvirt.VIR_CONNECT_LIST_NODE_DEVICES_CAP_PCI_DEV):
            try:
                descriptor = _parse_pci_descriptor(device.XMLDesc())
            except ET.ParseError:
                _log.warning(
                    "skipping unparseable PCI node-device XML for %s",
                    device.name(),
                    exc_info=True,
                )
                continue
            if descriptor is not None:
                descriptors.append(descriptor)
        return descriptors

    def list_owned(self) -> list[OwnedInfra]:
        """Return ``{system_id, domain_name}`` for each kdive-owned domain.

        Ownership is the kdive metadata tag when present, else the ``kdive-<uuid>`` naming
        convention (ADR-0111): a convention-named domain whose tag is absent/empty/unparseable
        is surfaced with ``system_id=""`` (the on-the-wire ``None``) so the reconciler falls
        back to the name and can reap a genuinely orphaned domain. A domain that is neither
        tagged nor convention-named is not ours and is skipped.
        """
        conn = self._connect()
        owned: list[OwnedInfra] = []
        for domain in conn.listAllDomains():
            entry = self._owned_entry(domain)
            if entry is not None:
                owned.append(entry)
        return owned

    def _owned_entry(self, domain: _LibvirtDomain) -> OwnedInfra | None:
        """Resolve one domain to an ``OwnedInfra`` row, or ``None`` when it is not ours."""
        name = domain.name()
        try:
            meta = domain.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, KDIVE_METADATA_NS, 0)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
                return self._name_fallback_entry(name)  # no tag → try the naming convention
            raise CategorizedError(
                "libvirt error reading domain metadata",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"domain": name},
            ) from exc
        system_id = parse_metadata_system_id(meta)
        if system_id is None:
            return self._name_fallback_entry(name)  # empty/malformed tag → naming convention
        return OwnedInfra(system_id=system_id, domain_name=name)

    @staticmethod
    def _name_fallback_entry(name: str) -> OwnedInfra | None:
        """A convention-named domain with no usable tag is ours (``system_id=""``); else skip."""
        if system_id_from_domain_name(name) is None:
            return None  # not a kdive System domain → not ours
        return OwnedInfra(system_id="", domain_name=name)
