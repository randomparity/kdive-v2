"""Rootfs resolver (ADR-0048 §5)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import _CatalogRootfs, _PathRootfs, _UploadRootfs, _UrlRootfs
from kdive.providers.local_libvirt.provisioning import (
    reject_rootfs_without_upload_window,
    resolve_rootfs_path,
    validate_rootfs_reference,
)

_SID = uuid4()


def test_path_passthrough() -> None:
    r = _PathRootfs(kind="path", path="/img/x.qcow2")
    assert resolve_rootfs_path(r, tenant="local", system_id=_SID) == "/img/x.qcow2"


def test_upload_uses_system_keyed_path() -> None:
    r = _UploadRootfs(kind="upload")
    assert str(_SID) in resolve_rootfs_path(r, tenant="local", system_id=_SID)


def test_url_bad_checksum_rejected() -> None:
    r = _UrlRootfs(kind="url", url="https://h/i.qcow2", sha256="deadbeef")
    with pytest.raises(CategorizedError) as e:
        resolve_rootfs_path(r, tenant="local", system_id=_SID)
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_unknown_catalog_rejected() -> None:
    r = _CatalogRootfs(kind="catalog", name="no-such")
    with pytest.raises(CategorizedError) as e:
        resolve_rootfs_path(r, tenant="local", system_id=_SID)
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


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
    reject_rootfs_without_upload_window(_PathRootfs(kind="path", path="/img/x.qcow2"))  # no raise


def test_validate_rootfs_reference_rejects_bad_url_checksum_at_tool_boundary() -> None:
    with pytest.raises(CategorizedError) as e:
        validate_rootfs_reference(_UrlRootfs(kind="url", url="https://h/i.qcow2", sha256="nope"))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_rootfs_reference_rejects_unknown_catalog_at_tool_boundary() -> None:
    with pytest.raises(CategorizedError) as e:
        validate_rootfs_reference(_CatalogRootfs(kind="catalog", name="no-such"))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR
