"""The manifest aggregates provider settings regardless of process (ADR-0087)."""

from __future__ import annotations

import kdive.config as config


def test_provider_settings_present_without_enabling_them() -> None:
    # A server process never imports the providers, but the manifest force-loads their
    # lightweight settings modules, so every variable is visible (reference completeness).
    names = {s.name for s in config.all_settings()}
    assert "KDIVE_LIBVIRT_URI" in names
    assert "KDIVE_FAULT_INJECT_SEED" in names
    assert "KDIVE_REMOTE_LIBVIRT_STORAGE_POOL" in names


def test_remote_libvirt_connection_singletons_are_gone() -> None:
    # The connection identity moved to the systems.toml [[remote_libvirt]] instance (#395); only
    # the libvirt host knobs the v2 model omits remain env settings.
    names = {s.name for s in config.all_settings()}
    assert "KDIVE_REMOTE_LIBVIRT_URI" not in names
    assert "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF" not in names
    assert "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF" not in names
    assert "KDIVE_REMOTE_LIBVIRT_GDB_ADDR" not in names


def test_no_duplicate_setting_names() -> None:
    names = [s.name for s in config.all_settings()]
    assert len(names) == len(set(names))
