# Provisioning-profile Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a versioned, immutable Pydantic `ProvisioningProfile` (provider-agnostic
core + a libvirt provider section) with a parse boundary that maps any structural
failure onto `configuration_error`, per ADR-0011 and ADR-0024.

**Architecture:** One module, `src/kdive/profiles/provisioning.py`, holds a closed
`BootMethod` enum, a `NonEmptyStr` constrained-string alias, and three nested
`frozen`/`extra="forbid"` models (`ProvisioningProfile` → `ProviderSection` →
`LibvirtProfile`). A `ProvisioningProfile.parse(data)` classmethod is the sanctioned
entry point: it calls `model_validate` and re-raises Pydantic's `ValidationError` as a
`CategorizedError(CONFIGURATION_ERROR)` with input-scrubbed details. The package
`__init__` re-exports the public names. `System.provisioning_profile` is **not**
rewired in this issue (ADR-0024 decision 6).

**Tech Stack:** Python 3.13, Pydantic 2.13.4, pytest, `uv`, `ruff`, `ty`.

---

## File Structure

- `src/kdive/profiles/provisioning.py` — **create.** The enum, the `NonEmptyStr`
  alias, the three models, and the `parse` boundary. Single responsibility: the
  provisioning-profile schema and its parse contract.
- `src/kdive/profiles/__init__.py` — **modify** (currently empty). Re-export the
  public names.
- `tests/profiles/__init__.py` — **create** (empty package marker; mirrors
  `tests/providers/__init__.py`).
- `tests/profiles/test_provisioning.py` — **create.** Behavior + edge/error tests.

### Reference conventions (already in the codebase)

- `src/kdive/domain/errors.py` — `CategorizedError(message, *, category, details)`
  and `ErrorCategory.CONFIGURATION_ERROR`.
- `src/kdive/domain/models.py` — `ResourceKind.LOCAL_LIBVIRT == "local-libvirt"`;
  `_DomainBase` shows the `ConfigDict(extra="forbid", …)` idiom and
  `Field(default_factory=dict)` usage.
- `src/kdive/store/objectstore.py`, `src/kdive/domain/allocation_admission.py` —
  the "map a failure onto `configuration_error` at a boundary" pattern.

---

## Task 1: Core models and the happy path

**Files:**
- Create: `src/kdive/profiles/provisioning.py`
- Create: `tests/profiles/__init__.py`
- Test: `tests/profiles/test_provisioning.py`

- [ ] **Step 1: Create the empty test package marker**

Create `tests/profiles/__init__.py` with no content (mirrors
`tests/providers/__init__.py`).

```python
```

- [ ] **Step 2: Write the failing happy-path test**

Create `tests/profiles/test_provisioning.py`:

```python
"""Tests for the provisioning-profile schema (`kdive.profiles.provisioning`)."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import (
    BootMethod,
    ProvisioningProfile,
)

_VALID: dict[str, object] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "pc-q35-9.0"},
            "rootfs_image_ref": "oci://registry.internal/rootfs/fedora-40@sha256:abc123",
            "crashkernel": "256M",
        }
    },
}


def _valid() -> dict[str, object]:
    """A fresh deep copy of the canonical valid profile, safe to mutate."""
    return copy.deepcopy(_VALID)


def test_valid_libvirt_profile_parses() -> None:
    profile = ProvisioningProfile.parse(_valid())

    assert profile.schema_version == 1
    assert profile.arch == "x86_64"
    assert profile.vcpu == 4
    assert profile.memory_mb == 4096
    assert profile.disk_gb == 20
    assert profile.boot_method is BootMethod.DIRECT_KERNEL
    assert profile.kernel_source_ref.startswith("git+https://")
    assert profile.provider.local_libvirt.domain_xml_params == {"machine": "pc-q35-9.0"}
    assert profile.provider.local_libvirt.rootfs_image_ref.startswith("oci://")


def test_crashkernel_is_present(  # kdump prerequisite (acceptance criterion)
) -> None:
    profile = ProvisioningProfile.parse(_valid())

    assert profile.provider.local_libvirt.crashkernel == "256M"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.profiles.provisioning'`.

- [ ] **Step 4: Write the module (models only, no `parse` yet — the test uses `parse`, so add it here)**

Create `src/kdive/profiles/provisioning.py`:

```python
"""The provisioning-profile schema and its parse boundary (ADR-0011, ADR-0024).

A provisioning profile is a versioned, declarative document with a
provider-agnostic core (target arch, vCPU, memory, disk, boot method, kernel-source
reference) and a provider-specific section keyed by provider name. M0 ships the
``local-libvirt`` variant only.

The models are ``frozen`` (the immutable-request-inputs invariant, ADR-0003/0011)
and reject unknown fields. ``ProvisioningProfile.parse`` is the sanctioned entry
point: it maps Pydantic's structural ``ValidationError`` onto the wire taxonomy's
``configuration_error`` and scrubs submitted values out of the error details so a
profile that references secret or guest-derived material cannot leak it (ADR-0024
decision 3). Constructing a model directly bypasses this mapping and is a caller
error.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind

type NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""A string that is non-empty after whitespace stripping; blank values fail validation."""


class BootMethod(StrEnum):
    """The provider-agnostic boot methods; M0 ships one (ADR-0024 decision 2a)."""

    DIRECT_KERNEL = "direct-kernel"


class _ProfileBase(BaseModel):
    """Shared config: reject unknown fields and freeze after construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class LibvirtProfile(_ProfileBase):
    """The ``local-libvirt`` provider section (ADR-0024 decisions 1, 2b, 2c).

    ``domain_xml_params`` is an optionally-empty map whose *values* are non-empty;
    ``crashkernel`` is an opaque non-empty token (the kdump prerequisite — the booted
    kernel is the arbiter of its grammar).
    """

    domain_xml_params: dict[str, NonEmptyStr] = Field(default_factory=dict)
    rootfs_image_ref: NonEmptyStr
    crashkernel: NonEmptyStr


class ProviderSection(_ProfileBase):
    """The provider-specific section, keyed by provider name (ADR-0024 decision 1).

    M0 requires exactly the ``local-libvirt`` section; an unknown provider key is
    rejected by ``extra="forbid"``.
    """

    local_libvirt: LibvirtProfile = Field(alias=ResourceKind.LOCAL_LIBVIRT.value)


class ProvisioningProfile(_ProfileBase):
    """A versioned provisioning profile: agnostic core plus a provider section."""

    schema_version: Literal[1]
    arch: NonEmptyStr
    vcpu: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    disk_gb: int = Field(gt=0)
    boot_method: BootMethod
    kernel_source_ref: NonEmptyStr
    provider: ProviderSection

    @classmethod
    def parse(cls, data: Mapping[str, object]) -> ProvisioningProfile:
        """Validate a profile document, mapping any failure to ``configuration_error``.

        Args:
            data: The deserialized profile document (a mapping; YAML/JSON parsing is
                the caller's responsibility).

        Returns:
            The validated, frozen profile.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for any structural failure —
                missing/unknown field, wrong type, empty required string, unreadable
                schema version. The error details carry field locations, types, and
                messages, but never the submitted values.
        """
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            details: dict[str, object] = {
                "errors": exc.errors(
                    include_url=False, include_input=False, include_context=False
                ),
            }
            raise CategorizedError(
                "invalid provisioning profile",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details=details,
            ) from exc
```

- [ ] **Step 5: Run the happy-path tests to verify they pass**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Run guardrails on the new files**

Run: `uv run ruff format src/kdive/profiles/provisioning.py tests/profiles/test_provisioning.py && uv run ruff check src/kdive/profiles tests/profiles && uv run ty check src`
Expected: all clean, no warnings.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/profiles/provisioning.py tests/profiles/__init__.py tests/profiles/test_provisioning.py
git commit -m "feat(profiles): provisioning-profile schema with libvirt section (#15)"
```

---

## Task 2: The `configuration_error` boundary (missing/unknown fields)

**Files:**
- Test: `tests/profiles/test_provisioning.py`
- (Implementation already added in Task 1 — these tests characterize the boundary.)

- [ ] **Step 1: Write the failing missing/unknown-field tests**

Append to `tests/profiles/test_provisioning.py`:

```python
def _expect_configuration_error(data: dict[str, object]) -> None:
    """Assert that parsing ``data`` fails as a CONFIGURATION_ERROR."""
    with pytest.raises(CategorizedError) as caught:
        ProvisioningProfile.parse(data)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "field",
    ["schema_version", "arch", "vcpu", "memory_mb", "disk_gb", "boot_method",
     "kernel_source_ref", "provider"],
)
def test_missing_core_field_raises_configuration_error(field: str) -> None:
    data = _valid()
    del data[field]
    _expect_configuration_error(data)


@pytest.mark.parametrize("field", ["rootfs_image_ref", "crashkernel"])
def test_missing_libvirt_field_raises_configuration_error(field: str) -> None:
    data = _valid()
    del data["provider"]["local-libvirt"][field]  # type: ignore[index]
    _expect_configuration_error(data)


def test_unknown_top_level_field_rejected() -> None:
    data = _valid()
    data["unexpected"] = "x"
    _expect_configuration_error(data)


def test_unknown_provider_key_rejected() -> None:
    data = _valid()
    data["provider"]["cloud"] = {}  # type: ignore[index]
    _expect_configuration_error(data)


def test_unknown_libvirt_field_rejected() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["extra"] = "x"  # type: ignore[index]
    _expect_configuration_error(data)
```

- [ ] **Step 2: Run the tests to verify they pass against the Task-1 boundary**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py -q`
Expected: PASS. (These characterize the `parse` boundary and `extra="forbid"` added
in Task 1; if any FAIL, the boundary is wrong — fix `provisioning.py`, not the test.)

- [ ] **Step 3: Commit**

```bash
git add tests/profiles/test_provisioning.py
git commit -m "test(profiles): cover missing/unknown-field configuration_error mapping (#15)"
```

---

## Task 3: Edge and value-constraint coverage

**Files:**
- Test: `tests/profiles/test_provisioning.py`

- [ ] **Step 1: Write the failing edge/constraint tests**

Append to `tests/profiles/test_provisioning.py`:

```python
@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("field", ["arch", "kernel_source_ref"])
def test_blank_core_string_rejected(field: str, value: str) -> None:
    data = _valid()
    data[field] = value
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("field", ["rootfs_image_ref", "crashkernel"])
def test_blank_libvirt_string_rejected(field: str, value: str) -> None:
    data = _valid()
    data["provider"]["local-libvirt"][field] = value  # type: ignore[index]
    _expect_configuration_error(data)


@pytest.mark.parametrize(("field", "value"), [("vcpu", 0), ("memory_mb", -1), ("disk_gb", 0)])
def test_non_positive_int_rejected(field: str, value: int) -> None:
    data = _valid()
    data[field] = value
    _expect_configuration_error(data)


def test_empty_domain_xml_param_value_rejected() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["domain_xml_params"] = {"machine": ""}  # type: ignore[index]
    _expect_configuration_error(data)


def test_domain_xml_params_defaults_to_empty_map() -> None:
    data = _valid()
    del data["provider"]["local-libvirt"]["domain_xml_params"]  # type: ignore[index]

    profile = ProvisioningProfile.parse(data)

    assert profile.provider.local_libvirt.domain_xml_params == {}


def test_unknown_boot_method_rejected() -> None:
    data = _valid()
    data["boot_method"] = "iso"
    _expect_configuration_error(data)


def test_unreadable_schema_version_rejected() -> None:
    data = _valid()
    data["schema_version"] = 2
    _expect_configuration_error(data)


def test_error_details_do_not_leak_submitted_values() -> None:
    data = _valid()
    data["memory_mb"] = "S3CRET-LOOKING-VALUE"  # wrong type carrying a sentinel

    with pytest.raises(CategorizedError) as caught:
        ProvisioningProfile.parse(data)

    assert "S3CRET-LOOKING-VALUE" not in str(caught.value.details)


def test_profile_is_frozen() -> None:
    profile = ProvisioningProfile.parse(_valid())

    with pytest.raises(ValidationError):
        profile.arch = "aarch64"  # type: ignore[misc]


def test_direct_construction_bypasses_configuration_error_mapping() -> None:
    # model_validate is not the sanctioned door; it surfaces the raw ValidationError.
    with pytest.raises(ValidationError):
        ProvisioningProfile.model_validate({"schema_version": 1})
```

- [ ] **Step 2: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py -q`
Expected: PASS. If `test_blank_*` FAIL, `NonEmptyStr` is not applied to that field; if
`test_empty_domain_xml_param_value_rejected` FAILs, `domain_xml_params`'s value type
is not `NonEmptyStr`. Fix `provisioning.py`.

- [ ] **Step 3: Commit**

```bash
git add tests/profiles/test_provisioning.py
git commit -m "test(profiles): cover value constraints, freeze, and detail redaction (#15)"
```

---

## Task 4: Public exports and final guardrails

**Files:**
- Modify: `src/kdive/profiles/__init__.py`

- [ ] **Step 1: Write the failing import test**

Append to `tests/profiles/test_provisioning.py`:

```python
def test_public_names_exported_from_package() -> None:
    import kdive.profiles as profiles

    assert profiles.ProvisioningProfile is ProvisioningProfile
    assert profiles.BootMethod is BootMethod
    assert hasattr(profiles, "LibvirtProfile")
    assert hasattr(profiles, "ProviderSection")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py::test_public_names_exported_from_package -q`
Expected: FAIL — `AttributeError: module 'kdive.profiles' has no attribute 'ProvisioningProfile'`.

- [ ] **Step 3: Populate the package `__init__`**

Replace the contents of `src/kdive/profiles/__init__.py`:

```python
"""Declarative request profiles (provisioning, and later build)."""

from __future__ import annotations

from kdive.profiles.provisioning import (
    BootMethod,
    LibvirtProfile,
    ProviderSection,
    ProvisioningProfile,
)

__all__ = [
    "BootMethod",
    "LibvirtProfile",
    "ProviderSection",
    "ProvisioningProfile",
]
```

- [ ] **Step 4: Run the import test to verify it passes**

Run: `uv run python -m pytest tests/profiles/test_provisioning.py::test_public_names_exported_from_package -q`
Expected: PASS.

- [ ] **Step 5: Run the full guardrail suite**

Run: `uv run ruff format --check src/kdive/profiles tests/profiles && uv run ruff check src/kdive/profiles tests/profiles && uv run ty check src && uv run python -m pytest tests/profiles -q`
Expected: all clean; all profile tests pass; zero warnings.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/profiles/__init__.py tests/profiles/test_provisioning.py
git commit -m "feat(profiles): export provisioning-profile public names (#15)"
```

---

## Self-Review

**Spec coverage (ADR-0011 + ADR-0024):**
- Provider-agnostic core (arch, vcpu, memory_mb, disk_gb, boot_method, kernel_source_ref) → Task 1 model. ✓
- Provider section nested mapping with required alias-keyed `local-libvirt` (decision 1) → Task 1 `ProviderSection`; unknown-key rejection → Task 2. ✓
- Explicit units (decision 2) → Task 1 field names + Task 3 non-positive tests. ✓
- Closed `boot_method` enum (decision 2a) → Task 1 `BootMethod` + Task 3 unknown-method test. ✓
- Non-empty required strings incl. `crashkernel` (decision 2b) → `NonEmptyStr` in Task 1 + Task 3 blank tests. ✓
- `domain_xml_params: dict[str, NonEmptyStr]`, optional-empty map (decision 2c) → Task 1 + Task 3 empty-value & default-map tests. ✓
- Parse boundary maps `ValidationError → configuration_error` (decision 3) → Task 1 `parse` + Task 2 tests; redaction → Task 3 leak test; direct-construction caveat → Task 3 test. ✓
- Frozen immutability (decision 4) → Task 1 config + Task 3 freeze test. ✓
- Required `Literal[1]` version (decision 5) → Task 1 field + Task 3 wrong-version & Task 2 missing-version tests. ✓
- No `System.provisioning_profile` rewire (decision 6) → out of scope; no task touches `domain/models.py`. ✓
- Acceptance: valid profile parses (Task 1), missing field → configuration_error (Task 2), crashkernel present (Task 1). ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every command shows expected output.

**Type consistency:** `ProvisioningProfile.parse`, `BootMethod.DIRECT_KERNEL`,
`ProviderSection.local_libvirt`, `LibvirtProfile.{domain_xml_params,rootfs_image_ref,crashkernel}`,
and `NonEmptyStr` are used identically across tasks. The `_valid()` helper and
`_expect_configuration_error` helper are defined once (Tasks 1 and 2) and reused.
