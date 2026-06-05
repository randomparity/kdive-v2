"""Tests for the build-profile schema (`kdive.profiles.build`)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import BuildProfile, ServerBuildProfile

_VALID: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "config_ref": "file:///configs/x86_64-kdump.config",
    "patch_ref": "file:///patches/fix.patch",
}


def _valid() -> dict[str, Any]:
    """A fresh deep copy of the canonical valid profile, safe to mutate."""
    return copy.deepcopy(_VALID)


def _expect_configuration_error(data: Any) -> None:
    """Assert that parsing ``data`` fails as a CONFIGURATION_ERROR."""
    with pytest.raises(CategorizedError) as caught:
        BuildProfile.parse(data)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_valid_profile_parses() -> None:
    profile = BuildProfile.parse(_valid())
    assert isinstance(profile, ServerBuildProfile)

    assert profile.schema_version == 1
    assert profile.kernel_source_ref.startswith("git+https://")
    assert profile.config_ref.startswith("file://")
    assert profile.patch_ref == "file:///patches/fix.patch"


def test_patch_ref_defaults_to_none() -> None:
    data = _valid()
    del data["patch_ref"]

    profile = BuildProfile.parse(data)
    assert isinstance(profile, ServerBuildProfile)

    assert profile.patch_ref is None


@pytest.mark.parametrize("field", ["schema_version", "kernel_source_ref", "config_ref"])
def test_missing_required_field_raises_configuration_error(field: str) -> None:
    data = _valid()
    del data[field]
    _expect_configuration_error(data)


def test_unknown_field_rejected() -> None:
    data = _valid()
    data["unexpected"] = "x"
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("field", ["kernel_source_ref", "config_ref"])
def test_blank_required_string_rejected(field: str, value: str) -> None:
    data = _valid()
    data[field] = value
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_patch_ref_rejected(value: str) -> None:
    # patch_ref is optional, but when supplied it must be a non-empty token.
    data = _valid()
    data["patch_ref"] = value
    _expect_configuration_error(data)


@pytest.mark.parametrize("payload", [None, [], "not-a-mapping", 42])
def test_non_mapping_input_rejected(payload: Any) -> None:
    _expect_configuration_error(payload)


def test_unreadable_schema_version_rejected() -> None:
    data = _valid()
    data["schema_version"] = 2
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_non_int_schema_version_rejected(value: object) -> None:
    # A bool/str/float must not coerce to version 1 (the Literal[1] coercion trap).
    data = _valid()
    data["schema_version"] = value
    _expect_configuration_error(data)


def test_error_details_do_not_leak_submitted_values() -> None:
    data = _valid()
    data["schema_version"] = "S3CRET-LOOKING-VALUE"  # wrong type carrying a sentinel

    with pytest.raises(CategorizedError) as caught:
        BuildProfile.parse(data)

    assert "S3CRET-LOOKING-VALUE" not in str(caught.value.details)


def test_profile_is_frozen() -> None:
    profile = BuildProfile.parse(_valid())
    assert isinstance(profile, ServerBuildProfile)

    with pytest.raises(ValidationError):
        profile.kernel_source_ref = "other"


def test_direct_construction_bypasses_configuration_error_mapping() -> None:
    # The sanctioned door is BuildProfile.parse; constructing a concrete model directly
    # (here ServerBuildProfile, since BuildProfile is no longer a Pydantic model) surfaces
    # the raw ValidationError without the CONFIGURATION_ERROR mapping.
    with pytest.raises(ValidationError):
        ServerBuildProfile.model_validate({"schema_version": 1})


def test_public_names_exported_from_package() -> None:
    import kdive.profiles as profiles

    assert profiles.BuildProfile is BuildProfile
