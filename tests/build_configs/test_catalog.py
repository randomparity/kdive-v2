"""Unit tests for the build-config catalog repository (ADR-0096)."""

from __future__ import annotations

import hashlib

import pytest

from kdive.build_configs.catalog import BuildConfigEntry, parse_build_config_row
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_parse_build_config_row_round_trips_fields() -> None:
    entry = parse_build_config_row(
        {
            "name": "kdump",
            "object_key": "system/build-configs/kdump/kdump.config",
            "sha256": "abc",
            "description": "kdump options",
        }
    )
    assert entry == BuildConfigEntry(
        name="kdump",
        object_key="system/build-configs/kdump/kdump.config",
        sha256="abc",
        description="kdump options",
    )


def test_verify_sha256_rejects_mismatch() -> None:
    entry = BuildConfigEntry("kdump", "k", sha256="deadbeef", description="")
    with pytest.raises(CategorizedError) as exc:
        entry.verify_bytes(b"the wrong bytes")
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_verify_sha256_accepts_match() -> None:
    data = b"CONFIG_CRASH_DUMP=y\n"
    digest = hashlib.sha256(data).hexdigest()
    entry = BuildConfigEntry("kdump", "k", sha256=digest, description="")
    entry.verify_bytes(data)  # does not raise
