"""Shared libvirt XML contract helpers."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from kdive.providers.libvirt_xml import (
    KDIVE_METADATA_NS,
    parse_capabilities_arch,
    parse_metadata_system_id,
    register_kdive_namespace,
)


def test_parse_capabilities_arch_reads_host_cpu_arch() -> None:
    xml = "<capabilities><host><cpu><arch>x86_64</arch></cpu></host></capabilities>"
    assert parse_capabilities_arch(xml) == "x86_64"


def test_parse_capabilities_arch_returns_unknown_for_missing_or_malformed() -> None:
    assert parse_capabilities_arch("<capabilities><host /></capabilities>") == "unknown"
    assert parse_capabilities_arch("<not-xml") == "unknown"


def test_parse_metadata_system_id_trims_text_and_rejects_empty_or_malformed() -> None:
    assert parse_metadata_system_id(f"<system xmlns='{KDIVE_METADATA_NS}'> sid </system>") == "sid"
    assert parse_metadata_system_id(f"<system xmlns='{KDIVE_METADATA_NS}' />") is None
    assert parse_metadata_system_id("<system") is None


def test_register_kdive_namespace_is_idempotent(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_register_namespace(prefix: str, uri: str) -> None:
        calls.append((prefix, uri))

    monkeypatch.setattr("kdive.providers.libvirt_xml._kdive_namespace_registered", False)
    monkeypatch.setattr(ET, "register_namespace", fake_register_namespace)
    register_kdive_namespace()
    register_kdive_namespace()
    assert calls == [("kdive", KDIVE_METADATA_NS)]
