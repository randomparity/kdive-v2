"""Source-discriminated build profile (ADR-0048 §2)."""

from __future__ import annotations

import pytest

from kdive.components.references import LocalComponentRef
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import (
    BuildProfile,
    ExternalBuildProfile,
    ServerBuildProfile,
)


def test_parse_defaults_to_server() -> None:
    parsed = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": "git#v6.9",
            "config": {"kind": "local", "path": "/configs/kernel.config"},
        }
    )
    assert isinstance(parsed, ServerBuildProfile)
    assert parsed.source == "server"
    assert isinstance(parsed.config, LocalComponentRef)
    assert parsed.config.path == "/configs/kernel.config"


def test_parse_external_requires_no_source_tree_fields() -> None:
    parsed = BuildProfile.parse({"schema_version": 1, "source": "external"})
    assert isinstance(parsed, ExternalBuildProfile)
    assert parsed.source == "external"


def test_external_profile_rejects_server_fields() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        BuildProfile.parse(
            {
                "schema_version": 1,
                "source": "external",
                "config": {"kind": "local", "path": "/configs/kernel.config"},
            }
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_server_profile_requires_config() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        BuildProfile.parse({"schema_version": 1, "kernel_source_ref": "git#v6.9"})
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_unknown_source_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        BuildProfile.parse({"schema_version": 1, "source": "bogus"})
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
