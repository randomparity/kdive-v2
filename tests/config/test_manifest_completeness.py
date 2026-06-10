"""The manifest aggregates provider settings regardless of process (ADR-0087)."""

from __future__ import annotations

import kdive.config as config


def test_provider_settings_present_without_enabling_them() -> None:
    # A server process never imports the providers, but the manifest force-loads their
    # lightweight settings modules, so every variable is visible (reference completeness).
    names = {s.name for s in config.all_settings()}
    assert "KDIVE_LIBVIRT_URI" in names
    assert "KDIVE_FAULT_INJECT_SEED" in names
    assert "KDIVE_REMOTE_LIBVIRT_URI" in names
    assert "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF" in names


def test_no_duplicate_setting_names() -> None:
    names = [s.name for s in config.all_settings()]
    assert len(names) == len(set(names))


def test_remote_cert_refs_are_conditionally_required() -> None:
    by_name = {s.name for s in config.all_settings()}
    assert "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF" in by_name
    ca = next(s for s in config.all_settings() if s.name == "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF")
    assert ca.required_when({"KDIVE_REMOTE_LIBVIRT_URI": "qemu+tls://h/system"}) is True
    assert ca.required_when({}) is False
