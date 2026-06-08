"""Rootfs upload-window guards (ADR-0048 §5)."""

from __future__ import annotations

import pytest

from kdive.components.references import CatalogComponentRef, LocalComponentRef
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import _UploadRootfs, validate_rootfs_reference
from kdive.providers.local_libvirt.provisioning import reject_rootfs_without_upload_window


def test_validate_rootfs_reference_accepts_well_formed_upload() -> None:
    # upload is well-formed (no fields to check); the worker's render path must accept it
    # so an admitted DEFINED System can render (#111). Lane admissibility is a separate guard.
    validate_rootfs_reference(_UploadRootfs(kind="upload"))  # does not raise


def test_reject_rootfs_without_upload_window_rejects_upload() -> None:
    # The one-step provision / reprovision lanes have no upload window, so an upload
    # reference there can never have a staged object — fail fast (#111).
    with pytest.raises(CategorizedError) as e:
        reject_rootfs_without_upload_window(_UploadRootfs(kind="upload"))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_reject_rootfs_without_upload_window_allows_path() -> None:
    reject_rootfs_without_upload_window(
        LocalComponentRef(kind="local", path="/img/x.qcow2")
    )  # no raise


def test_validate_rootfs_reference_accepts_local_at_tool_boundary() -> None:
    validate_rootfs_reference(LocalComponentRef(kind="local", path="/img/x.qcow2"))


def test_validate_rootfs_reference_rejects_unknown_catalog_at_tool_boundary() -> None:
    with pytest.raises(CategorizedError) as e:
        validate_rootfs_reference(
            CatalogComponentRef(kind="catalog", provider="local-libvirt", name="no-such")
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR
