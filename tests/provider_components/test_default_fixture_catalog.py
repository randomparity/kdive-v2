from pathlib import Path

import pytest

from kdive.images.seed import PACKAGED_SEED_DATA_PATH
from kdive.provider_components.catalog import DEFAULT_FIXTURE_CATALOG_PATH, load_fixture_catalog


def test_packaged_baseline_exposes_expected_rootfs_entries() -> None:
    # The rootfs catalog moved to the packaged seed_data baseline (ADR-0092); the source-tree
    # fixtures hold only profiles now.
    catalog = load_fixture_catalog(PACKAGED_SEED_DATA_PATH)
    names = {entry.name for entry in catalog.rootfs_for_provider("local-libvirt")}
    assert "fedora-kdive-ready-43" in names
    assert "fedora-cloud-43" in names
    assert "busybox-bare" in names


def test_packaged_baseline_rootfs_entries_are_qcow2_vda() -> None:
    catalog = load_fixture_catalog(PACKAGED_SEED_DATA_PATH)
    for name in ("fedora-kdive-ready-43", "fedora-cloud-43", "busybox-bare"):
        entry = catalog.rootfs_entry("local-libvirt", name)
        assert entry is not None
        assert entry.format == "qcow2"
        assert entry.root_device == "/dev/vda"
        assert entry.source.kind == "local"


def test_default_fixture_catalog_has_no_rootfs_entries() -> None:
    # The default fixture catalog keeps only profiles; rootfs moved to seed_data.
    catalog = load_fixture_catalog(DEFAULT_FIXTURE_CATALOG_PATH)
    assert catalog.rootfs == []
    assert catalog.profiles != []


def test_catalog_path_can_be_overridden_by_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "catalog"
    (fixture / "rootfs").mkdir(parents=True)
    (fixture / "manifest.yaml").write_text(
        "schema_version: 1\n"
        "provider: local-libvirt\n"
        "storage:\n"
        "  allowed_component_roots: [/tmp/rootfs]\n"
        "  cache_dir: /tmp/rootfs/cache\n"
        "  overlay_dir: /tmp/rootfs/overlays\n"
        "rootfs: [rootfs/custom.yaml]\n"
        "profiles: []\n",
        encoding="utf-8",
    )
    (fixture / "rootfs" / "custom.yaml").write_text(
        "provider: local-libvirt\n"
        "name: custom-rootfs\n"
        "arch: x86_64\n"
        "format: qcow2\n"
        "root_device: /dev/vda\n"
        "source:\n"
        "  kind: local\n"
        "  path: /tmp/rootfs/custom.qcow2\n"
        "visibility: public\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KDIVE_FIXTURE_CATALOG_PATH", str(fixture))

    catalog = load_fixture_catalog()

    assert catalog.rootfs_entry("local-libvirt", "custom-rootfs") is not None
