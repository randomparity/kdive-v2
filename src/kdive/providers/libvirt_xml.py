"""Shared libvirt XML contract helpers for provider implementations."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

KDIVE_METADATA_NS = "https://kdive.dev/libvirt/1"
QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"

_kdive_namespace_registered = False
_qemu_namespace_registered = False


def register_kdive_namespace() -> None:
    """Register the ``kdive`` XML prefix before rendering domain metadata."""
    global _kdive_namespace_registered
    if _kdive_namespace_registered:
        return
    ET.register_namespace("kdive", KDIVE_METADATA_NS)
    _kdive_namespace_registered = True


def register_qemu_namespace() -> None:
    """Register the ``qemu`` XML prefix before rendering qemu commandline elements."""
    global _qemu_namespace_registered
    if _qemu_namespace_registered:
        return
    ET.register_namespace("qemu", QEMU_NS)
    _qemu_namespace_registered = True


def parse_capabilities_arch(caps_xml: str) -> str:
    """Read ``<host><cpu><arch>`` from libvirt capabilities XML; ``unknown`` if malformed."""
    try:
        root: ET.Element = _safe_fromstring(caps_xml)
    except (ET.ParseError, DefusedXmlException):
        return "unknown"
    return root.findtext("./host/cpu/arch") or "unknown"


def parse_metadata_system_id(meta_xml: str) -> str | None:
    """Read the System id from a kdive metadata XML element; ``None`` if empty/malformed."""
    try:
        element: ET.Element = _safe_fromstring(meta_xml)
    except (ET.ParseError, DefusedXmlException):
        return None
    text = (element.text or "").strip()
    return text or None
