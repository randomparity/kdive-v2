"""Ported rootfs catalog (ADR-0048 §3/§5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.rootfs.catalog import RootfsCatalog, load_catalog


def test_bundled_catalog_loads_and_validates() -> None:
    catalog = load_catalog()
    assert isinstance(catalog, RootfsCatalog)
    assert catalog.entries  # at least one curated image


def test_lookup_known_and_unknown() -> None:
    catalog = load_catalog()
    first = catalog.entries[0]
    assert catalog.lookup(first.name) is first
    assert catalog.lookup("no-such-image") is None


def test_entry_rejects_non_https_url() -> None:
    from kdive.rootfs.catalog import CatalogEntry

    with pytest.raises(ValueError):
        CatalogEntry.model_validate(
            {
                "name": "x",
                "distro": "fedora",
                "release": "43",
                "architecture": "x86_64",
                "url": "http://insecure/img.qcow2",
                "checksum": "sha256:" + "0" * 64,
                "size_bytes": 1,
                "image_format": "qcow2",
                "readiness_marker": "login:",
                "ssh_user": "fedora",
            }
        )


def test_entry_rejects_bad_checksum() -> None:
    from kdive.rootfs.catalog import CatalogEntry

    with pytest.raises(ValueError):
        CatalogEntry.model_validate(
            {
                "name": "x",
                "distro": "fedora",
                "release": "43",
                "architecture": "x86_64",
                "url": "https://h/img.qcow2",
                "checksum": "deadbeef",
                "size_bytes": 1,
                "image_format": "qcow2",
                "readiness_marker": "login:",
                "ssh_user": "fedora",
            }
        )


def test_load_catalog_missing_file_maps_to_infrastructure_failure() -> None:
    with pytest.raises(CategorizedError) as exc_info:
        load_catalog(Path("/nonexistent/catalog.json"))
    assert exc_info.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
