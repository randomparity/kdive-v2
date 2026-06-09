from __future__ import annotations

from pathlib import Path

from kdive.provider_components.catalog import load_fixture_catalog


def test_load_fixture_catalog_filters_provider(tmp_path: Path) -> None:
    fixture = tmp_path / "local-libvirt"
    (fixture / "rootfs").mkdir(parents=True)
    (fixture / "profiles").mkdir()
    (fixture / "configs").mkdir()
    (fixture / "manifest.yaml").write_text(
        "schema_version: 1\n"
        "provider: local-libvirt\n"
        "storage:\n"
        "  allowed_component_roots: [/var/lib/kdive/rootfs]\n"
        "  cache_dir: /var/lib/kdive/rootfs/cache\n"
        "  overlay_dir: /var/lib/kdive/rootfs/overlays\n"
        "rootfs: [rootfs/base.yaml]\n"
        "profiles: [profiles/console.yaml]\n",
        encoding="utf-8",
    )
    (fixture / "rootfs" / "base.yaml").write_text(
        "provider: local-libvirt\n"
        "name: base\n"
        "arch: x86_64\n"
        "format: qcow2\n"
        "root_device: /dev/vda\n"
        "source:\n"
        "  kind: local\n"
        "  path: /var/lib/kdive/rootfs/base.qcow2\n"
        "visibility: public\n"
        "capabilities: [kdive-ready-console]\n",
        encoding="utf-8",
    )
    (fixture / "profiles" / "console.yaml").write_text(
        "provider: local-libvirt\n"
        "name: console-ready_x86_64\n"
        "arch: x86_64\n"
        "requires:\n"
        "  config:\n"
        "    required: {CONFIG_VIRTIO_BLK: y}\n"
        "  cmdline:\n"
        "    required_tokens: [console=ttyS0]\n"
        "    protected_prefixes: [console=]\n"
        "  rootfs:\n"
        "    format: qcow2\n"
        "    root_device: /dev/vda\n"
        "    capabilities: [kdive-ready-console]\n",
        encoding="utf-8",
    )

    catalog = load_fixture_catalog(fixture)

    assert [entry.name for entry in catalog.rootfs_for_provider("local-libvirt")] == ["base"]
    assert catalog.rootfs_for_provider("remote-libvirt") == []
    assert catalog.profile("local-libvirt", "console-ready_x86_64") is not None


def test_fixture_catalog_hides_non_public_rootfs_entries(tmp_path: Path) -> None:
    fixture = tmp_path / "local-libvirt"
    (fixture / "rootfs").mkdir(parents=True)
    (fixture / "manifest.yaml").write_text(
        "schema_version: 1\n"
        "provider: local-libvirt\n"
        "storage:\n"
        "  allowed_component_roots: [/var/lib/kdive/rootfs]\n"
        "  cache_dir: /var/lib/kdive/rootfs/cache\n"
        "  overlay_dir: /var/lib/kdive/rootfs/overlays\n"
        "rootfs: [rootfs/public.yaml, rootfs/project.yaml, rootfs/host-policy.yaml]\n"
        "profiles: []\n",
        encoding="utf-8",
    )
    rootfs_template = (
        "provider: local-libvirt\n"
        "name: {name}\n"
        "arch: x86_64\n"
        "format: qcow2\n"
        "root_device: /dev/vda\n"
        "source:\n"
        "  kind: local\n"
        "  path: /var/lib/kdive/rootfs/{name}.qcow2\n"
        "visibility: {visibility}\n"
        "capabilities: [kdive-ready-console]\n"
    )
    (fixture / "rootfs" / "public.yaml").write_text(
        rootfs_template.format(name="public-base", visibility="public"),
        encoding="utf-8",
    )
    (fixture / "rootfs" / "project.yaml").write_text(
        rootfs_template.format(name="project-base", visibility="project"),
        encoding="utf-8",
    )
    (fixture / "rootfs" / "host-policy.yaml").write_text(
        rootfs_template.format(name="host-policy-base", visibility="host-policy"),
        encoding="utf-8",
    )

    catalog = load_fixture_catalog(fixture)

    assert [entry.name for entry in catalog.rootfs_for_provider("local-libvirt")] == ["public-base"]
    assert catalog.rootfs_entry("local-libvirt", "public-base") is not None
    assert catalog.rootfs_entry("local-libvirt", "project-base") is None
    assert catalog.rootfs_entry("local-libvirt", "host-policy-base") is None
