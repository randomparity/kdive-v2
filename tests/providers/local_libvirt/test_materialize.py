from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import _UploadRootfs
from kdive.provider_components.references import CatalogComponentRef, LocalComponentRef
from kdive.providers.local_libvirt.materialize import (
    RootfsMaterializationContext,
    RootfsUploadContext,
    materialize_rootfs_base,
)


def test_materialize_local_rootfs_validates_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"
    root.mkdir()
    image = root / "base.qcow2"
    image.write_bytes(b"data")

    result = materialize_rootfs_base(
        LocalComponentRef(kind="local", path=str(image)),
        context=RootfsMaterializationContext(allowed_roots=[root]),
    )

    assert result == image.resolve()


def test_materialize_catalog_rootfs_uses_injected_fetch(tmp_path: Path) -> None:
    # The `catalog` path cuts over to the DB resolver + object fetch (ADR-0092): the context
    # supplies a fetch that returns the checksum-verified local cache path.
    cached = tmp_path / "abc.qcow2"
    cached.write_bytes(b"data")
    seen: list[CatalogComponentRef] = []

    def _fetch(ref: CatalogComponentRef) -> Path:
        seen.append(ref)
        return cached

    ref = CatalogComponentRef(kind="catalog", provider="local-libvirt", name="base")
    result = materialize_rootfs_base(
        ref,
        context=RootfsMaterializationContext(allowed_roots=[tmp_path], catalog_fetch=_fetch),
    )

    assert result == cached
    assert seen == [ref]


def test_materialize_catalog_rootfs_unwired_lane_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as error:
        materialize_rootfs_base(
            CatalogComponentRef(kind="catalog", provider="local-libvirt", name="base"),
            context=RootfsMaterializationContext(allowed_roots=[tmp_path]),
        )

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_materialize_uploaded_rootfs_uses_system_keyed_path(tmp_path: Path) -> None:
    system_id = uuid4()

    result = materialize_rootfs_base(
        _UploadRootfs(kind="upload"),
        context=RootfsMaterializationContext(
            allowed_roots=[tmp_path],
            upload=RootfsUploadContext("local", system_id, tmp_path),
        ),
    )

    assert result == tmp_path / f"local-systems-{system_id}-rootfs.qcow2"


def test_materialize_uploaded_rootfs_requires_system_context(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as error:
        materialize_rootfs_base(
            _UploadRootfs(kind="upload"),
            context=RootfsMaterializationContext(allowed_roots=[tmp_path]),
        )

    assert error.value.category is ErrorCategory.CONFIGURATION_ERROR
