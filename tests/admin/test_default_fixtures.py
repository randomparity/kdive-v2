"""Default fixture bundle tests."""

from __future__ import annotations

import yaml

from kdive.admin.default_fixtures import LOCAL_LIBVIRT_FIXTURES


def test_local_libvirt_fixtures_declare_manifest_and_profile() -> None:
    manifest = yaml.safe_load(LOCAL_LIBVIRT_FIXTURES["manifest.yaml"])

    assert manifest["schema_version"] == 1
    assert manifest["provider"] == "local-libvirt"
    assert manifest["rootfs"] == []
    assert manifest["profiles"] == ["profiles/console-ready_x86_64.yaml"]
    assert manifest["storage"]["allowed_component_roots"] == ["/var/lib/kdive/rootfs"]


def test_console_ready_profile_carries_required_boot_policy() -> None:
    profile = yaml.safe_load(LOCAL_LIBVIRT_FIXTURES["profiles/console-ready_x86_64.yaml"])

    assert profile["provider"] == "local-libvirt"
    assert profile["name"] == "console-ready_x86_64"
    assert profile["requires"]["cmdline"]["required_tokens"] == [
        "console=ttyS0",
        "root=/dev/vda",
    ]
    assert "kdive-ready-console" in profile["requires"]["rootfs"]["capabilities"]
