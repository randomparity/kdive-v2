"""Local-libvirt Discovery plane (ADR-0023).

`LocalLibvirtDiscovery` enumerates the local libvirt host over an **injected**
connection factory (so unit tests never touch a real host; the real `libvirt.open`
adapter is `live_vm`-only) and advertises arch/cpu/memory, a `gdbstub` transport, and
the per-host concurrent-Allocation cap.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import libvirt
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import ResourceStatus
from kdive.providers.interfaces import OwnedInfra, ResourceRecord

_KDIVE_METADATA_NS = "https://kdive.dev/libvirt/1"
_URI_ENV = "KDIVE_LIBVIRT_URI"
_CAP_ENV = "KDIVE_LIBVIRT_ALLOCATION_CAP"
_DEFAULT_CAP = 1


class _LibvirtDomain(Protocol):
    def name(self) -> str: ...
    def metadata(self, kind: int, uri: str | None, flags: int) -> str: ...


class _LibvirtConn(Protocol):
    def getInfo(self) -> list[Any]: ...
    def getCapabilities(self) -> str: ...
    def listAllDomains(self, flags: int = 0) -> Sequence[_LibvirtDomain]: ...


type Connect = Callable[[], _LibvirtConn]


def _parse_arch(caps_xml: str) -> str:
    """Read ``<host><cpu><arch>`` from the capabilities XML; ``unknown`` if absent.

    Parsed with ``defusedxml`` — the XML crosses a trust boundary (it is emitted by the
    libvirtd process), so entity-expansion DoS (billion-laughs) is neutralized; a
    malformed document returns ``unknown``, an *attack* document raises (fail loud).
    """
    try:
        root: ET.Element = _safe_fromstring(caps_xml)
    except ET.ParseError:
        return "unknown"
    return root.findtext("./host/cpu/arch") or "unknown"


def _parse_system_id(meta_xml: str) -> str | None:
    """Read the System uuid from a kdive metadata element; ``None`` if empty/malformed.

    ``defusedxml`` parse (trust boundary, as ``_parse_arch``): malformed → ``None``;
    an attack document raises rather than being silently skipped as "untagged".
    """
    try:
        element: ET.Element = _safe_fromstring(meta_xml)
    except ET.ParseError:
        return None
    text = (element.text or "").strip()
    return text or None


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
        host_uri = os.environ.get(_URI_ENV, "qemu:///system")
        raw_cap = os.environ.get(_CAP_ENV)
        if raw_cap is None:
            cap = _DEFAULT_CAP
        else:
            try:
                cap = int(raw_cap)
            except ValueError:
                raise CategorizedError(
                    f"{_CAP_ENV}={raw_cap!r} is not an integer",
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
            "arch": _parse_arch(conn.getCapabilities()),
            "vcpus": int(info[2]),
            "memory_mb": int(info[1]),
            "transports": ["gdbstub"],
            CONCURRENT_ALLOCATION_CAP_KEY: self.concurrent_allocation_cap,
        }
        return [
            ResourceRecord(
                resource_id=self.host_uri,
                kind=ResourceKind.LOCAL_LIBVIRT,
                capabilities=capabilities,
                status=ResourceStatus.AVAILABLE,
            )
        ]

    def list_owned(self) -> list[OwnedInfra]:
        """Return `{system_id, domain_name}` for each kdive-tagged domain."""
        conn = self._connect()
        owned: list[OwnedInfra] = []
        for domain in conn.listAllDomains():
            try:
                meta = domain.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, _KDIVE_METADATA_NS, 0)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
                    continue  # untagged → not ours
                raise CategorizedError(
                    "libvirt error reading domain metadata",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"domain": domain.name()},
                ) from exc
            system_id = _parse_system_id(meta)
            if system_id is None:
                continue
            owned.append(OwnedInfra(system_id=system_id, domain_name=domain.name()))
        return owned
