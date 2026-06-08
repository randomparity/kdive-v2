from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from kdive.components.references import CatalogComponentRef, LocalComponentRef
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import _UploadRootfs
from kdive.providers.local_libvirt.materialize import materialize_rootfs_base


def test_materialize_local_rootfs_validates_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"
    root.mkdir()
    image = root / "base.qcow2"
    image.write_bytes(b"data")

    result = materialize_rootfs_base(
        LocalComponentRef(kind="local", path=str(image)),
        allowed_roots=[root],
        cache_dir=tmp_path / "cache",
        project="proj-a",
        component_store=None,
        object_store=None,
    )

    assert result == image.resolve()


def test_materialize_local_backed_catalog_rootfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "local-libvirt"
    root = tmp_path / "rootfs"
    image = root / "base.qcow2"
    root.mkdir()
    image.write_bytes(b"data")
    (fixture / "rootfs").mkdir(parents=True)
    (fixture / "profiles").mkdir()
    (fixture / "manifest.yaml").write_text(
        "schema_version: 1\n"
        "provider: local-libvirt\n"
        "storage:\n"
        f"  allowed_component_roots: [{root}]\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"  overlay_dir: {tmp_path / 'overlays'}\n"
        "rootfs: [rootfs/base.yaml]\n"
        "profiles: []\n",
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
        f"  path: {image}\n"
        "visibility: public\n"
        "capabilities: [kdive-ready-console]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KDIVE_FIXTURE_CATALOG_PATH", str(fixture))

    result = materialize_rootfs_base(
        CatalogComponentRef(kind="catalog", provider="local-libvirt", name="base"),
        allowed_roots=[root],
        cache_dir=tmp_path / "cache",
        project="proj-a",
        component_store=None,
        object_store=None,
    )

    assert result == image.resolve()


def test_materialize_uploaded_rootfs_uses_system_keyed_path(tmp_path: Path) -> None:
    system_id = uuid4()

    result = materialize_rootfs_base(
        _UploadRootfs(kind="upload"),
        allowed_roots=[tmp_path],
        cache_dir=tmp_path / "cache",
        project="proj-a",
        system_id=system_id,
        upload_dir=tmp_path,
        component_store=None,
        object_store=None,
    )

    assert result == tmp_path / f"local-systems-{system_id}-rootfs.qcow2"


def test_materialize_uploaded_rootfs_requires_system_context(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as error:
        materialize_rootfs_base(
            _UploadRootfs(kind="upload"),
            allowed_roots=[tmp_path],
            cache_dir=tmp_path / "cache",
            project="proj-a",
            component_store=None,
            object_store=None,
        )

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_materialize_host_policy_catalog_rootfs_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "local-libvirt"
    root = tmp_path / "rootfs"
    image = root / "base.qcow2"
    root.mkdir()
    image.write_bytes(b"data")
    (fixture / "rootfs").mkdir(parents=True)
    (fixture / "profiles").mkdir()
    (fixture / "manifest.yaml").write_text(
        "schema_version: 1\n"
        "provider: local-libvirt\n"
        "storage:\n"
        f"  allowed_component_roots: [{root}]\n"
        f"  cache_dir: {tmp_path / 'cache'}\n"
        f"  overlay_dir: {tmp_path / 'overlays'}\n"
        "rootfs: [rootfs/base.yaml]\n"
        "profiles: []\n",
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
        f"  path: {image}\n"
        "visibility: host-policy\n"
        "capabilities: [kdive-ready-console]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KDIVE_FIXTURE_CATALOG_PATH", str(fixture))

    with pytest.raises(CategorizedError) as error:
        materialize_rootfs_base(
            CatalogComponentRef(kind="catalog", provider="local-libvirt", name="base"),
            allowed_roots=[root],
            cache_dir=tmp_path / "cache",
            project="proj-a",
            component_store=None,
            object_store=None,
        )

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
