# Provider Component Fixtures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement ADR-0065's provider component references, provider-scoped rootfs catalog,
profile requirements, and local-libvirt fixture bootstrap so an agent can supply or select the
kernel, config, initrd, rootfs, and command line that local-libvirt validates and consumes.

**Architecture:** Replace the current rootfs/config assumptions with a shared component-reference
model, a provider-scoped fixture catalog, local-libvirt validators/materializers, and MCP tools for
agent discovery/preflight. Keep the first runtime target local-libvirt; remote-provider behavior is
modeled only as source-kind rejection and artifact-reference privacy rules.

**Tech Stack:** Python 3.13, Pydantic, psycopg, FastMCP, libvirt/qemu-img seams, `uv`, `pytest`,
`ruff`, `ty`, `shellcheck`, `shfmt`.

**ADR:** [`../../adr/0065-provider-component-references.md`](../../adr/0065-provider-component-references.md)
· **Spec:** [`../specs/2026-06-07-local-libvirt-fixture-design.md`](../specs/2026-06-07-local-libvirt-fixture-design.md)

---

## File Structure

- **Create** `src/kdive/components/references.py`: typed `local`, `artifact`, and `catalog`
  component-reference models plus parse helpers.
- **Create** `src/kdive/components/local_paths.py`: provider-local path validation under allowed
  roots, including symlink escape rejection, regular-file checks, readability checks, and optional
  sha256 verification.
- **Create** `src/kdive/components/requirements.py`: config and command-line requirement models
  and validators.
- **Create** `src/kdive/components/catalog.py`: provider-scoped rootfs/profile catalog loader for
  fixture YAML files.
- **Modify** `pyproject.toml` and `uv.lock`: add direct `pyyaml==6.0.3` dependency because
  `kdive.provider_components.catalog` imports `yaml` directly.
- **Create** `src/kdive/components/validation.py`: component-source validation service
  that checks accepted component source kinds and dispatches profile requirement checks.
- **Modify** `src/kdive/providers/composition.py`: expose local-libvirt component capabilities and
  the validation service through `ProviderRuntime`.
- **Modify** `src/kdive/profiles/provisioning.py`: replace legacy rootfs source variants with
  ADR-0065 component references where the provider profile names rootfs inputs.
- **Modify** `src/kdive/profiles/build.py`: distinguish agent complete config refs from profile
  requirements; keep server/external build lanes.
- **Modify** `src/kdive/providers/local_libvirt/provisioning.py`: resolve `local`, `artifact`, and
  `catalog` rootfs references through the component resolver/materializer before overlay creation.
- **Modify** `src/kdive/providers/local_libvirt/build.py`: normalize and validate the agent's
  complete `.config` against selected profile requirements before producing artifacts.
- **Create** `src/kdive/providers/local_libvirt/materialize.py`: local-backed rootfs validation,
  content-addressed artifact cache helpers, and overlay base selection.
- **Create** `src/kdive/mcp/tools/catalog/rootfs.py`: `rootfs.catalog_list` and
  `rootfs.catalog_get`.
- **Create** `src/kdive/mcp/tools/catalog/profiles.py`: `profiles.catalog_list` and
  `profiles.catalog_get`.
- **Create** `src/kdive/mcp/tools/catalog/components.py`: `components.link_local`,
  `components.get`, `components.create_upload`, and `components.finalize_upload`.
- **Create** `src/kdive/mcp/tools/providers/validation.py`: `providers.validate_components`.
- **Modify** `src/kdive/mcp/app.py`: register the new catalog/provider tool modules.
- **Create** `src/kdive/db/schema/0009_provider_components.sql`: durable linked-local and
  artifact-backed component metadata.
- **Create** `src/kdive/db/provider_components.py`: query helpers for component links and catalog
  visibility.
- **Create** `fixtures/local-libvirt/`: manifest, rootfs metadata, profile metadata, and additive
  `.required.config` fragments.
- **Create** `scripts/fixtures/local-libvirt`: operator helper for `prepare`, `verify`, and `env`.
- **Modify** docs named in the spec's "Current Documentation To Supersede" section after runtime
  behavior changes.

---

## Milestone 1: Component Reference Models

**Files:**
- Create: `src/kdive/components/__init__.py`
- Create: `src/kdive/components/references.py`
- Test: `tests/provider_components/test_references.py`

- [ ] **Step 1: Write failing tests for the three reference kinds**

Create `tests/provider_components/test_references.py`:

```python
from __future__ import annotations

import pytest

from kdive.provider_components.references import (
    ArtifactComponentRef,
    CatalogComponentRef,
    LocalComponentRef,
    parse_component_ref,
)
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_parse_local_ref_requires_absolute_path() -> None:
    ref = parse_component_ref(
        {"kind": "local", "path": "/var/lib/kdive/rootfs/base.qcow2", "sha256": "sha256:" + "0" * 64}
    )
    assert isinstance(ref, LocalComponentRef)
    assert ref.path == "/var/lib/kdive/rootfs/base.qcow2"


def test_parse_artifact_ref() -> None:
    ref = parse_component_ref(
        {"kind": "artifact", "artifact_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert isinstance(ref, ArtifactComponentRef)


def test_parse_catalog_ref() -> None:
    ref = parse_component_ref({"kind": "catalog", "provider": "local-libvirt", "name": "fedora"})
    assert isinstance(ref, CatalogComponentRef)
    assert ref.provider == "local-libvirt"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "local", "path": "relative.img"},
        {"kind": "local", "path": "/x", "sha256": "deadbeef"},
        {"kind": "artifact", "artifact_id": "not-a-uuid"},
        {"kind": "catalog", "provider": "remote-libvirt", "name": ""},
        {"kind": "url", "url": "https://example.invalid/x.qcow2"},
    ],
)
def test_parse_component_ref_maps_invalid_payloads_to_config_error(payload: dict[str, object]) -> None:
    with pytest.raises(CategorizedError) as caught:
        parse_component_ref(payload)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run the tests and confirm the missing module failure**

Run: `uv run python -m pytest tests/provider_components/test_references.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.provider_components'`.

- [ ] **Step 3: Implement the reference models**

Create `src/kdive/components/__init__.py` as an empty package marker.

Create `src/kdive/components/references.py`:

```python
"""Provider component reference models (ADR-0065)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from kdive.domain.errors import CategorizedError, ErrorCategory

type NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}\\Z")


class _ComponentRefBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalComponentRef(_ComponentRefBase):
    kind: Literal["local"]
    path: NonEmptyStr
    sha256: str | None = None

    @field_validator("path")
    @classmethod
    def _validate_absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("local component path must be absolute")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.match(value):
            raise ValueError("sha256 must be 'sha256:<64 lowercase hex chars>'")
        return value


class ArtifactComponentRef(_ComponentRefBase):
    kind: Literal["artifact"]
    artifact_id: UUID
    sha256: str | None = None

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.match(value):
            raise ValueError("sha256 must be 'sha256:<64 lowercase hex chars>'")
        return value


class CatalogComponentRef(_ComponentRefBase):
    kind: Literal["catalog"]
    provider: NonEmptyStr
    name: NonEmptyStr


type ComponentRef = Annotated[
    LocalComponentRef | ArtifactComponentRef | CatalogComponentRef,
    Field(discriminator="kind"),
]


class _ComponentRefAdapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: ComponentRef


def parse_component_ref(data: Mapping[str, object]) -> ComponentRef:
    """Parse one component reference and map structural errors to KDIVE error taxonomy."""
    try:
        return _ComponentRefAdapter.model_validate({"ref": data}).ref
    except ValidationError as exc:
        raise CategorizedError(
            "invalid component reference",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"errors": exc.errors(include_url=False, include_input=False, include_context=False)},
        ) from exc
```

- [ ] **Step 4: Run focused tests**

Run: `uv run python -m pytest tests/provider_components/test_references.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/components tests/provider_components/test_references.py
git commit -m "feat: add provider component reference models"
```

---

## Milestone 2: Local Path Validation

**Files:**
- Create: `src/kdive/components/local_paths.py`
- Test: `tests/provider_components/test_local_paths.py`

- [ ] **Step 1: Write failing tests for provider-local paths**

Create `tests/provider_components/test_local_paths.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from kdive.provider_components.local_paths import validate_local_component_path
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_accepts_regular_file_under_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    image = root / "base.qcow2"
    image.write_bytes(b"data")

    result = validate_local_component_path(str(image), allowed_roots=[root])

    assert result == image.resolve()


def test_rejects_path_outside_allowed_roots(tmp_path: Path) -> None:
    outside = tmp_path / "outside.qcow2"
    outside.write_bytes(b"data")

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(outside), allowed_roots=[tmp_path / "root"])
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside.qcow2"
    root.mkdir()
    outside.write_bytes(b"data")
    (root / "link.qcow2").symlink_to(outside)

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(root / "link.qcow2"), allowed_roots=[root])
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_rejects_sha256_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    image = root / "base.qcow2"
    image.write_bytes(b"data")

    with pytest.raises(CategorizedError) as caught:
        validate_local_component_path(str(image), allowed_roots=[root], sha256="sha256:" + "0" * 64)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run the tests and confirm the missing function failure**

Run: `uv run python -m pytest tests/provider_components/test_local_paths.py -q`

Expected: FAIL with `ModuleNotFoundError` or missing import for `validate_local_component_path`.

- [ ] **Step 3: Implement local path validation**

Create `src/kdive/components/local_paths.py`:

```python
"""Provider-local component path validation (ADR-0065)."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory


def validate_local_component_path(
    path: str,
    *,
    allowed_roots: Iterable[Path],
    sha256: str | None = None,
) -> Path:
    """Return a resolved regular file path after provider-root and digest validation."""
    candidate = Path(path)
    if not candidate.is_absolute():
        raise _config_error("local component path must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _config_error("local component path does not exist") from exc
    roots = [root.resolve(strict=False) for root in allowed_roots]
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise _config_error("local component path is outside provider allowed roots")
    if not resolved.is_file():
        raise _config_error("local component path is not a regular file")
    if not os.access(resolved, os.R_OK):
        raise _config_error("local component path is not readable")
    if sha256 is not None and _file_sha256(resolved) != sha256.removeprefix("sha256:"):
        raise _config_error("local component sha256 does not match")
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)
```

- [ ] **Step 4: Run focused tests**

Run: `uv run python -m pytest tests/provider_components/test_local_paths.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/components/local_paths.py tests/provider_components/test_local_paths.py
git commit -m "feat: validate provider-local component paths"
```

---

## Milestone 3: Profile Requirements

**Files:**
- Create: `src/kdive/components/requirements.py`
- Test: `tests/provider_components/test_requirements.py`

- [ ] **Step 1: Write failing tests for config and command-line checks**

Create `tests/provider_components/test_requirements.py`:

```python
from __future__ import annotations

import pytest

from kdive.provider_components.requirements import (
    CmdlineRequirements,
    ConfigRequirements,
    validate_cmdline_requirements,
    validate_config_requirements,
)
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_config_requirements_accept_matching_values() -> None:
    validate_config_requirements(
        "CONFIG_VIRTIO_BLK=y\nCONFIG_DEBUG_INFO=y\n",
        ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "y"}),
    )


def test_config_requirements_reject_missing_value() -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_config_requirements("", ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "y"}))
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_cmdline_requirements_accept_required_tokens() -> None:
    validate_cmdline_requirements(
        "console=ttyS0 root=/dev/vda dhash_entries=1",
        CmdlineRequirements(required_tokens=["console=ttyS0", "root=/dev/vda"]),
        platform_cmdline="console=ttyS0 root=/dev/vda",
    )


def test_cmdline_requirements_rejects_protected_override() -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_cmdline_requirements(
            "console=tty0 root=/dev/vda",
            CmdlineRequirements(required_tokens=["root=/dev/vda"], protected_prefixes=["console="]),
            platform_cmdline="console=ttyS0 root=/dev/vda",
        )
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run the tests and confirm missing module failure**

Run: `uv run python -m pytest tests/provider_components/test_requirements.py -q`

Expected: FAIL with missing `kdive.provider_components.requirements`.

- [ ] **Step 3: Implement requirement models and validators**

Create `src/kdive/components/requirements.py`:

```python
"""Provider/profile requirement validators (ADR-0065)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from kdive.domain.errors import CategorizedError, ErrorCategory


class ConfigRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required: dict[str, str] = Field(default_factory=dict)


class CmdlineRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_tokens: list[str] = Field(default_factory=list)
    protected_prefixes: list[str] = Field(default_factory=list)


def validate_config_requirements(config_text: str, requirements: ConfigRequirements) -> None:
    values = _parse_config(config_text)
    missing = {
        key: value
        for key, value in requirements.required.items()
        if values.get(key) != value
    }
    if missing:
        raise CategorizedError(
            "kernel config does not satisfy profile requirements",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"missing_or_different": sorted(missing)},
        )


def validate_cmdline_requirements(
    cmdline: str,
    requirements: CmdlineRequirements,
    *,
    platform_cmdline: str,
) -> None:
    tokens = cmdline.split()
    missing = [token for token in requirements.required_tokens if token not in tokens]
    if missing:
        raise CategorizedError(
            "kernel command line does not include required tokens",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"missing": missing},
        )
    platform = {prefix: _first_token_with_prefix(platform_cmdline, prefix) for prefix in requirements.protected_prefixes}
    supplied = {prefix: _first_token_with_prefix(cmdline, prefix) for prefix in requirements.protected_prefixes}
    overrides = [
        prefix
        for prefix, platform_token in platform.items()
        if platform_token is not None and supplied[prefix] is not None and supplied[prefix] != platform_token
    ]
    if overrides:
        raise CategorizedError(
            "kernel command line overrides protected platform tokens",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"protected_prefixes": overrides},
        )


def _parse_config(config_text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in config_text.splitlines():
        if line.startswith("# CONFIG_") and line.endswith(" is not set"):
            key = line.removeprefix("# ").removesuffix(" is not set")
            values[key] = "n"
            continue
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _first_token_with_prefix(cmdline: str, prefix: str) -> str | None:
    for token in cmdline.split():
        if token.startswith(prefix):
            return token
    return None
```

- [ ] **Step 4: Run focused tests**

Run: `uv run python -m pytest tests/provider_components/test_requirements.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/components/requirements.py tests/provider_components/test_requirements.py
git commit -m "feat: validate provider profile requirements"
```

---

## Milestone 4: Provider-Scoped Fixture Catalog

**Files:**
- Create: `src/kdive/components/catalog.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `fixtures/local-libvirt/manifest.yaml`
- Create: `fixtures/local-libvirt/rootfs/fedora-kdive-ready-43.yaml`
- Create: `fixtures/local-libvirt/profiles/console-ready-x86_64.yaml`
- Create: `fixtures/local-libvirt/configs/console-ready.required.config`
- Test: `tests/provider_components/test_catalog.py`

- [ ] **Step 1: Write failing catalog tests**

Create `tests/provider_components/test_catalog.py`:

```python
from __future__ import annotations

from pathlib import Path

from kdive.provider_components.catalog import load_fixture_catalog


def test_load_fixture_catalog_filters_provider(tmp_path: Path) -> None:
    fixture = tmp_path / "local-libvirt"
    (fixture / "rootfs").mkdir(parents=True)
    (fixture / "profiles").mkdir()
    (fixture / "configs").mkdir()
    (fixture / "manifest.yaml").write_text(
        "schema_version: 1\n"
        "provider: local-libvirt\n"
        "storage:\n"
        "  allowed_component_roots: [/var/lib/kdive/rootfs]\n"
        "  cache_dir: /var/lib/kdive/rootfs/cache\n"
        "  overlay_dir: /var/lib/kdive/rootfs/overlays\n"
        "rootfs: [rootfs/base.yaml]\n"
        "profiles: [profiles/console.yaml]\n"
    )
    (fixture / "rootfs" / "base.yaml").write_text(
        "provider: local-libvirt\n"
        "name: base\n"
        "arch: x86_64\n"
        "format: qcow2\n"
        "root_device: /dev/vda\n"
        "source:\n"
        "  kind: local\n"
        "  path: /var/lib/kdive/rootfs/base.qcow2\n"
        "visibility: public\n"
        "capabilities: [kdive-ready-console]\n"
    )
    (fixture / "profiles" / "console.yaml").write_text(
        "provider: local-libvirt\n"
        "name: console-ready_x86_64\n"
        "arch: x86_64\n"
        "requires:\n"
        "  config:\n"
        "    required: {CONFIG_VIRTIO_BLK: y}\n"
        "  cmdline:\n"
        "    required_tokens: [console=ttyS0]\n"
        "    protected_prefixes: [console=]\n"
        "  rootfs:\n"
        "    format: qcow2\n"
        "    root_device: /dev/vda\n"
        "    capabilities: [kdive-ready-console]\n"
    )

    catalog = load_fixture_catalog(fixture)

    assert [entry.name for entry in catalog.rootfs_for_provider("local-libvirt")] == ["base"]
    assert catalog.rootfs_for_provider("remote-libvirt") == []
    assert catalog.profile("local-libvirt", "console-ready_x86_64") is not None
```

- [ ] **Step 2: Run the test and confirm missing loader failure**

Run: `uv run python -m pytest tests/provider_components/test_catalog.py -q`

Expected: FAIL with missing `kdive.provider_components.catalog`.

- [ ] **Step 3: Implement YAML-backed fixture catalog models**

Add PyYAML as a direct runtime dependency before importing it:

```bash
uv add pyyaml==6.0.3
```

PyPI lists `6.0.3` as the current stable PyYAML release as of June 7, 2026, and it already
appears in `uv.lock` transitively. The implementation should make it direct because this module
imports `yaml`.

Create `src/kdive/components/catalog.py` with frozen Pydantic models shaped like this:

```python
class FixtureStorage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_component_roots: list[Path]
    cache_dir: Path
    overlay_dir: Path


class RootfsRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["qcow2"]
    root_device: str
    capabilities: list[str] = Field(default_factory=list)


class ProfileRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config: ConfigRequirements = Field(default_factory=ConfigRequirements)
    cmdline: CmdlineRequirements = Field(default_factory=CmdlineRequirements)
    rootfs: RootfsRequirements


class FixtureManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    provider: str
    storage: FixtureStorage
    rootfs: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)


class RootfsCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    name: str
    arch: str
    format: Literal["qcow2"]
    root_device: str
    source: ComponentRef
    visibility: Literal["public", "project", "host-policy"]
    capabilities: list[str] = Field(default_factory=list)


class ProfileCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    name: str
    arch: str
    requires: ProfileRequirements


class FixtureCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: FixtureManifest
    rootfs: list[RootfsCatalogEntry]
    profiles: list[ProfileCatalogEntry]

    def rootfs_for_provider(self, provider: str) -> list[RootfsCatalogEntry]:
        return [entry for entry in self.rootfs if entry.provider == provider]

    def profile(self, provider: str, name: str) -> ProfileCatalogEntry | None:
        for entry in self.profiles:
            if entry.provider == provider and entry.name == name:
                return entry
        return None
```

Import `ConfigRequirements` and `CmdlineRequirements` from
`kdive.provider_components.requirements`, and import `ComponentRef` from
`kdive.provider_components.references`. Use `yaml.safe_load`; map file-read, YAML, and
model-validation failures to a `CategorizedError` with
`category=ErrorCategory.INFRASTRUCTURE_FAILURE`
because fixture data packaged in the repo is operator-owned configuration.

- [ ] **Step 4: Add the first local-libvirt fixture files**

Create `fixtures/local-libvirt/manifest.yaml`, `rootfs/fedora-kdive-ready-43.yaml`,
`profiles/console-ready_x86_64.yaml`, and `configs/console-ready.required.config` using the
schema from the spec. Use a local-backed placeholder rootfs path under
`/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2`; the fixture `verify` command will
report it missing until the operator builds, imports, or links it.

- [ ] **Step 5: Run focused tests**

Run: `uv run python -m pytest tests/provider_components/test_catalog.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/components/catalog.py tests/provider_components/test_catalog.py fixtures/local-libvirt
git commit -m "feat: add provider-scoped fixture catalog"
```

---

## Milestone 5: Provider Capabilities And Preflight Validation

**Files:**
- Create: `src/kdive/components/validation.py`
- Modify: `src/kdive/providers/composition.py`
- Test: `tests/provider_components/test_validation.py`
- Test: `tests/providers/test_composition.py`

- [ ] **Step 1: Write failing tests for component/source support**

Create `tests/provider_components/test_validation.py`:

```python
from __future__ import annotations

import pytest

from kdive.provider_components.references import ArtifactComponentRef, LocalComponentRef
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.validation import (
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)


def test_accepts_supported_component_source() -> None:
    caps = ComponentSourceCapabilities(
        provider="local-libvirt",
        accepted_component_sources={"rootfs": frozenset({"local"})},
    )
    reject_unsupported_component_source(
        caps,
        component_kind="rootfs",
        ref=LocalComponentRef(kind="local", path="/var/lib/kdive/rootfs/base.qcow2"),
    )


def test_rejects_remote_provider_local_source() -> None:
    caps = ComponentSourceCapabilities(
        provider="remote-libvirt",
        accepted_component_sources={"rootfs": frozenset({"artifact", "catalog"})},
    )
    with pytest.raises(CategorizedError) as caught:
        reject_unsupported_component_source(
            caps,
            component_kind="rootfs",
            ref=LocalComponentRef(kind="local", path="/var/lib/kdive/rootfs/base.qcow2"),
        )
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_rejects_unimplemented_local_libvirt_kernel_artifact_source() -> None:
    caps = ComponentSourceCapabilities(
        provider="local-libvirt",
        accepted_component_sources={"kernel": frozenset({"local"})},
    )
    with pytest.raises(CategorizedError) as caught:
        reject_unsupported_component_source(
            caps,
            component_kind="kernel",
            ref=ArtifactComponentRef(
                kind="artifact",
                artifact_id="00000000-0000-0000-0000-000000000000",
            ),
        )
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run the test and confirm missing module failure**

Run: `uv run python -m pytest tests/provider_components/test_validation.py -q`

Expected: FAIL with missing `kdive.provider_components.validation`.

- [ ] **Step 3: Implement capability models and rejection helper**

Create `src/kdive/components/validation.py` with `ComponentSourceCapabilities` and
`reject_unsupported_component_source()`. The helper must include the `provider`,
`component_kind`, `source_kind`, and accepted source kinds in error details, without echoing local
paths.

- [ ] **Step 4: Wire local-libvirt capabilities into `ProviderRuntime`**

Modify `src/kdive/providers/composition.py`:

```python
component_sources: ComponentSourceCapabilities = field(
    default_factory=lambda: ComponentSourceCapabilities(
        provider=ResourceKind.LOCAL_LIBVIRT.value,
        accepted_component_sources={
            "rootfs": frozenset({"local"}),
            "kernel": frozenset({"local"}),
            "initrd": frozenset({"local"}),
            "config": frozenset({"local"}),
            "patch": frozenset({"local"}),
            "vmlinux": frozenset({"local"}),
        },
    )
)
```

Pass the same value explicitly from `build_default_provider_runtime()` so tests can assert the
runtime advertises only the source kinds implemented at this milestone. Do not advertise
artifact-backed or catalog-backed support before the materializer can consume it; an agent-visible
capability must not be a future promise.

- [ ] **Step 5: Run focused tests**

Run: `uv run python -m pytest tests/provider_components/test_validation.py tests/providers/test_composition.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/components/validation.py src/kdive/providers/composition.py tests/provider_components
git commit -m "feat: advertise provider component source support"
```

---

## Milestone 6: Local-Libvirt Local Rootfs Materialization

**Files:**
- Create: `src/kdive/providers/local_libvirt/materialize.py`
- Modify: `src/kdive/providers/local_libvirt/provisioning.py`
- Test: `tests/providers/local_libvirt/test_materialize.py`
- Test: `tests/providers/local_libvirt/test_rootfs_resolve.py`

- [ ] **Step 1: Write failing materialization tests**

Create `tests/providers/local_libvirt/test_materialize.py`:

```python
from __future__ import annotations

from pathlib import Path

from kdive.provider_components.references import LocalComponentRef
from kdive.providers.local_libvirt.lifecycle.materialize import materialize_rootfs_base


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
```

- [ ] **Step 2: Run the test and confirm missing module failure**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_materialize.py -q`

Expected: FAIL with missing `kdive.providers.local_libvirt.lifecycle.materialize`.

- [ ] **Step 3: Implement local-backed materialization**

Create `src/kdive/providers/local_libvirt/materialize.py` with:

```python
class ComponentStore(Protocol):
    def get_visible_component(self, component_id: UUID, *, project: str) -> ProviderComponent | None:
        """Return an authorized provider component or None."""


class ObjectStore(Protocol):
    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        """Fetch an authorized object-store artifact."""


def materialize_rootfs_base(
    ref: ComponentRef,
    *,
    allowed_roots: list[Path],
    cache_dir: Path,
    project: str,
    component_store: ComponentStore | None,
    object_store: ObjectStore | None,
) -> Path:
    if isinstance(ref, LocalComponentRef):
        return validate_local_component_path(ref.path, allowed_roots=allowed_roots, sha256=ref.sha256)
    if isinstance(ref, CatalogComponentRef):
        entry = load_fixture_catalog().rootfs_entry(ref.provider, ref.name)
        if isinstance(entry.source, LocalComponentRef):
            return validate_local_component_path(
                entry.source.path,
                allowed_roots=allowed_roots,
                sha256=entry.source.sha256,
            )
    if component_store is None or object_store is None:
        raise CategorizedError(
            "artifact-backed rootfs materialization is not wired yet",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
```

For this milestone, fully implement `LocalComponentRef` using
`validate_local_component_path()`. Resolve `CatalogComponentRef` only when the catalog entry's
source is `LocalComponentRef`, then validate that local path. Reject direct
`ArtifactComponentRef` and artifact-backed catalog entries with `MISSING_DEPENDENCY` until the
component upload/finalization and authorized object-store materialization milestone wires the
required stores. Keep the `project`, `component_store`, and `object_store` parameters in the first
slice so the later artifact-backed implementation can authorize every cache lookup without
changing the provisioning call shape again.

After local-backed catalog resolution passes, update `ProviderRuntime.component_sources` for
`rootfs` from `frozenset({"local"})` to `frozenset({"local", "catalog"})`. Add a composition test
that proves catalog rootfs is advertised only after this milestone's materializer branch exists.

- [ ] **Step 4: Replace legacy `path` resolution in provisioning**

Modify `src/kdive/profiles/provisioning.py` and
`src/kdive/providers/local_libvirt/provisioning.py` so local-libvirt rootfs references parse as
ADR-0065 component refs. Keep the existing `upload` lane only if it is still required by current
tests, but mark it as a System upload artifact path, not a provider catalog format.

- [ ] **Step 5: Run focused provider tests**

Run:

```bash
uv run python -m pytest \
  tests/providers/local_libvirt/test_materialize.py \
  tests/providers/local_libvirt/test_rootfs_resolve.py \
  tests/profiles/test_provisioning.py \
  -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/local_libvirt src/kdive/profiles/provisioning.py tests/providers/local_libvirt tests/profiles
git commit -m "feat: materialize local-libvirt rootfs component refs"
```

---

## Milestone 7: Build Config Normalization And Requirement Check

**Files:**
- Modify: `src/kdive/profiles/build.py`
- Modify: `src/kdive/providers/local_libvirt/build.py`
- Test: `tests/profiles/test_build.py`
- Test: `tests/providers/local_libvirt/test_build.py`

- [ ] **Step 1: Write failing tests for complete config plus requirements**

Add tests to `tests/profiles/test_build.py` and `tests/providers/local_libvirt/test_build.py`.

For the profile schema, assert this payload parses:

```python
profile = BuildProfile.parse(
    {
        "schema_version": 1,
        "source": "server",
        "kernel_source_ref": "file:///home/dave/src/linux",
        "config": {"kind": "local", "path": "/var/lib/kdive/components/linux.config"},
        "profile_requirements": {
            "provider": "local-libvirt",
            "name": "console-ready_x86_64",
        },
    }
)
assert isinstance(profile, ServerBuildProfile)
assert profile.config.kind == "local"
assert profile.profile_requirements.name == "console-ready_x86_64"
```

For the builder, monkeypatch checkout/build seams so no compiler runs. Make the staged
workspace `.config` contain `CONFIG_VIRTIO_BLK=n`, select a requirement that needs
`CONFIG_VIRTIO_BLK=y`, call `build()`, and assert a `CONFIGURATION_ERROR` is raised before the
artifact store is written.

Add external-build tests in `tests/providers/local_libvirt/test_validate_external_artifacts.py`
and `tests/mcp/lifecycle/test_complete_build_tool.py`:

- a manifest with `kernel` and `effective_config` passes only when `effective_config` satisfies the
  selected profile requirements
- a manifest without `effective_config` is rejected when the external build profile names
  `profile_requirements`
- a mismatching `effective_config` maps to `CONFIGURATION_ERROR` before artifact rows are committed

- [ ] **Step 2: Run the focused tests**

Run:

```bash
uv run python -m pytest \
  tests/profiles/test_build.py \
  tests/providers/local_libvirt/test_build.py \
  -q
```

Expected: FAIL with missing field/model behavior.

- [ ] **Step 3: Update build profile schema**

Replace `config_ref` semantics with `config` as a component ref for the complete agent config and
`profile_requirements` as a provider/profile selector. For external-build profiles, add
`profile_requirements` without `config`; the effective `.config` must arrive as an uploaded
`effective_config` artifact during `runs.complete_build`. Keep this as a replace operation in docs
and tests; do not add a compatibility shim for the old complete-config assumption unless a
still-active runtime path cannot be updated in the same task.

- [ ] **Step 4: Validate config after kernel-tree normalization**

In `LocalLibvirtBuild`, stage the complete config, run the target tree's config update step already
used by the build lane, read the resulting workspace `.config`, and call
`validate_config_requirements()` with the selected provider profile requirements.

In `validate_external_artifacts()`, accept `effective_config` as a build-upload artifact name,
fetch it through the object-store seam, and call `validate_config_requirements()` before returning
`ValidatedUpload`. `runs.complete_build` must pass the parsed external build profile's
`profile_requirements` into the validator and must not write artifact rows when validation fails.

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run python -m pytest \
  tests/profiles/test_build.py \
  tests/providers/local_libvirt/test_build.py \
  tests/mcp/lifecycle/test_complete_build_tool.py \
  -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/profiles/build.py src/kdive/providers/local_libvirt/build.py tests/profiles tests/providers/local_libvirt tests/mcp/lifecycle
git commit -m "feat: validate build configs against profile requirements"
```

---

## Milestone 8: Durable Component Links, Uploads, And Visibility

**Files:**
- Create: `src/kdive/db/schema/0009_provider_components.sql`
- Create: `src/kdive/db/provider_components.py`
- Test: `tests/db/test_provider_components.py`

- [ ] **Step 1: Write failing DB tests**

Create `tests/db/test_provider_components.py` with tests for:

```python
async def test_project_component_visible_only_to_same_project(pool: AsyncConnectionPool) -> None:
    component_id = await link_local_component(
        pool,
        provider="local-libvirt",
        component_kind="rootfs",
        path="/var/lib/kdive/rootfs/local/base.qcow2",
        sha256="sha256:" + "0" * 64,
        visibility="project",
        project="proj-a",
        principal="alice",
    )

    same_project = await list_visible_components(
        pool, provider="local-libvirt", component_kind="rootfs", project="proj-a"
    )
    other_project = await list_visible_components(
        pool, provider="local-libvirt", component_kind="rootfs", project="proj-b"
    )

    assert [component.id for component in same_project] == [component_id]
    assert other_project == []
```

Add a second test that calls `get_visible_component()` with `project="proj-b"` and asserts
`None`; call it with `project="proj-a"` and assert the returned source kind is `local`.

Add a third test that creates an artifact-backed component row with an `artifact_id`, expected
sha256, `visibility="project"`, and `project="proj-a"`, then proves it is visible only to
`proj-a`. Do not store raw S3 keys in this table.

Add a fourth test that creates a component upload intent, verifies the returned presigned object key
is not persisted in `provider_components`, finalizes the upload into a provider component, and
asserts a second finalize call returns the already-created component id instead of duplicating it.
The test should also assert the finalize helper derives the object key from `component_uploads.id`
and does not require an object key column.

- [ ] **Step 2: Run the tests and confirm missing schema/helper failures**

Run: `uv run python -m pytest tests/db/test_provider_components.py -q`

Expected: FAIL because the table and helpers do not exist.

- [ ] **Step 3: Add schema**

Create `src/kdive/db/schema/0009_provider_components.sql` with:

```sql
CREATE TABLE provider_components (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider text NOT NULL,
    component_kind text NOT NULL,
    source jsonb NOT NULL,
    artifact_id uuid,
    visibility text NOT NULL CONSTRAINT provider_components_visibility_check
        CHECK (visibility IN ('public', 'project', 'host-policy')),
    project text,
    principal text NOT NULL,
    sha256 text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_components_project_visibility_check
        CHECK ((visibility = 'project' AND project IS NOT NULL) OR visibility <> 'project')
);
CREATE TRIGGER provider_components_set_updated_at BEFORE UPDATE ON provider_components
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();
CREATE INDEX provider_components_provider_kind_idx
    ON provider_components (provider, component_kind);
CREATE INDEX provider_components_project_idx ON provider_components (project);

CREATE TABLE component_uploads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider text NOT NULL,
    component_kind text NOT NULL,
    artifact_id uuid,
    sha256 text NOT NULL,
    size_bytes bigint NOT NULL CONSTRAINT component_uploads_size_positive_check
        CHECK (size_bytes > 0),
    visibility text NOT NULL CONSTRAINT component_uploads_visibility_check
        CHECK (visibility IN ('public', 'project')),
    project text NOT NULL,
    principal text NOT NULL,
    state text NOT NULL CONSTRAINT component_uploads_state_check
        CHECK (state IN ('pending', 'finalized', 'failed')),
    deadline timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER component_uploads_set_updated_at BEFORE UPDATE ON component_uploads
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();
CREATE INDEX component_uploads_project_state_idx ON component_uploads (project, state);
```

- [ ] **Step 4: Add query helpers**

Implement `link_local_component()`, `get_visible_component()`, and `list_visible_components()` in
`src/kdive/db/provider_components.py`. Parse `source` through `parse_component_ref()` on read so
bad rows fail as infrastructure defects during tests.

Also implement `create_component_upload_intent()`, `finalize_component_upload()`,
`create_artifact_component()`, and `component_upload_object_key()`. The key helper deterministically
derives the upload object key from tenant, provider, component kind, and `component_uploads.id`, so
finalization can verify the upload without persisting a raw object-store key. Finalization verifies
the uploaded object's etag, size, and sha256 through the object-store client, writes one
artifact-backed component row, stores the resulting artifact id on the upload row, marks the upload
`finalized`, and is idempotent on retry. These helpers store artifact ids and expected digests; they
never store or return object-store keys from the durable component registry.

- [ ] **Step 5: Run DB tests**

Run: `uv run python -m pytest tests/db/test_migrate.py tests/db/test_provider_components.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/db/schema/0009_provider_components.sql src/kdive/db/provider_components.py tests/db/test_provider_components.py
git commit -m "feat: store provider component links with visibility"
```

---

## Milestone 9: MCP Discovery And Validation Tools

**Files:**
- Create: `src/kdive/mcp/tools/catalog/rootfs.py`
- Create: `src/kdive/mcp/tools/catalog/profiles.py`
- Create: `src/kdive/mcp/tools/catalog/components.py`
- Create: `src/kdive/mcp/tools/providers/__init__.py`
- Create: `src/kdive/mcp/tools/providers/validation.py`
- Modify: `src/kdive/mcp/app.py`
- Test: `tests/mcp/catalog/test_rootfs_catalog_tools.py`
- Test: `tests/mcp/catalog/test_profile_catalog_tools.py`
- Test: `tests/mcp/catalog/test_components_tools.py`
- Test: `tests/mcp/providers/test_validation_tools.py`
- Test: `tests/mcp/core/test_app.py`

- [ ] **Step 1: Write failing MCP tool tests**

Add tests for:

- `rootfs.catalog_list(provider="local-libvirt")` returns only local-libvirt visible entries.
- `profiles.catalog_get(provider="local-libvirt", name="console-ready_x86_64")` returns profile
  requirements and no workflow/case metadata.
- `components.link_local(provider="local-libvirt", kind="rootfs", path="/var/lib/kdive/rootfs/local/base.qcow2", visibility="project")`
  stores a project-scoped linked local component after local path validation.
- `components.create_upload(kind="rootfs", provider="local-libvirt", sha256="sha256:<64-hex>", size_bytes=1073741824)`
  creates a private project-scoped component upload intent.
- `components.finalize_upload(component_upload_id="<uuid>")` verifies the uploaded object metadata
  and registers an artifact-backed component without exposing S3 keys.
- `providers.validate_components(provider="local-libvirt", profile="console-ready_x86_64",
  components={"rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/local/base.qcow2"}})`
  returns a success envelope for supported component refs and a configuration-error envelope for
  unsupported source kinds.

Use the existing MCP direct-handler test pattern: seed a `RequestContext` with project `proj-a`,
call the async handler function directly, and assert the `ToolResponse.status`,
`error_category`, and `suggested_next_actions` fields rather than asserting the FastMCP transport.

- [ ] **Step 2: Run the tests and confirm tool registration failures**

Run:

```bash
uv run python -m pytest \
  tests/mcp/catalog/test_rootfs_catalog_tools.py \
  tests/mcp/catalog/test_profile_catalog_tools.py \
  tests/mcp/catalog/test_components_tools.py \
  tests/mcp/providers/test_validation_tools.py \
  tests/mcp/core/test_app.py \
  -q
```

Expected: FAIL because the tools are not registered.

- [ ] **Step 3: Implement catalog tools**

Follow the existing thin-wrapper pattern in `src/kdive/mcp/tools/catalog/artifacts.py`: handlers
take `pool` and `RequestContext`, wrappers call `current_context()`, responses are `ToolResponse`
envelopes, and project/role checks happen before project-scoped entries are returned.

- [ ] **Step 4: Implement validation tool**

Use `ProviderRuntime.component_sources` and the validators from prior milestones. The tool is a
preflight convenience only; provider operations must keep their own enforcement.

- [ ] **Step 5: Register tools**

Append the new registrars to `_PLANE_REGISTRARS` in `src/kdive/mcp/app.py`.

- [ ] **Step 6: Run focused MCP tests**

Run the command from Step 2 again.

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/mcp/tools src/kdive/mcp/app.py tests/mcp
git commit -m "feat: expose provider component catalog tools"
```

---

## Milestone 10: Fixture Operator Script

**Files:**
- Create: `scripts/fixtures/local-libvirt`
- Test: `tests/scripts/test_local_libvirt_fixture_script.py`

- [ ] **Step 1: Write failing script tests**

Create `tests/scripts/test_local_libvirt_fixture_script.py` with subprocess tests that run:

```bash
scripts/fixtures/local-libvirt env
scripts/fixtures/local-libvirt verify --dry-run
```

Assert `env` prints `KDIVE_LIBVIRT_URI=qemu:///system`, `KDIVE_ROOTFS_CACHE_DIR`, and
`KDIVE_ROOTFS_OVERLAY_DIR`. Assert `verify --dry-run` reports missing rootfs files without
touching libvirt by setting `KDIVE_FIXTURE_NO_LIBVIRT=1` and checking the script exits with code
0 while printing `libvirt: skipped`.

- [ ] **Step 2: Run tests and confirm missing script failure**

Run: `uv run python -m pytest tests/scripts/test_local_libvirt_fixture_script.py -q`

Expected: FAIL because the script does not exist.

- [ ] **Step 3: Implement the script**

Create `scripts/fixtures/local-libvirt` with `set -euo pipefail`, subcommands `env`,
`prepare`, and `verify`, and no parsing of `docs/test-cases`. `prepare` creates directories and
the libvirt storage pool when not in `--dry-run`; `verify` checks commands, `/dev/kvm`, libvirt
connectivity, allowed roots, and catalog local paths.

- [ ] **Step 4: Lint shell and run focused tests**

Run:

```bash
shellcheck scripts/fixtures/local-libvirt
shfmt -d scripts/fixtures/local-libvirt
uv run python -m pytest tests/scripts/test_local_libvirt_fixture_script.py -q
```

Expected: PASS and no shell lint output.

- [ ] **Step 5: Commit**

```bash
git add scripts/fixtures/local-libvirt tests/scripts/test_local_libvirt_fixture_script.py
git commit -m "feat: add local-libvirt fixture helper"
```

---

## Milestone 11: Artifact-Backed Rootfs Materialization

**Files:**
- Modify: `src/kdive/providers/local_libvirt/materialize.py`
- Modify: `src/kdive/providers/local_libvirt/provisioning.py`
- Test: `tests/providers/local_libvirt/test_materialize.py`
- Test: `tests/providers/local_libvirt/test_rootfs_resolve.py`

- [ ] **Step 1: Write failing artifact materialization tests**

Add tests that cover:

- artifact-backed rootfs cache path derives from sha256:
  `/var/lib/kdive/rootfs/cache/sha256/<hex>.qcow2`
- an existing cache file is reused only after size, sha256, and qcow2 metadata validation
- a corrupt existing cache file is replaced through a `.part` download path
- an artifact-backed catalog entry is rejected when the caller's project cannot see the component
- a visible artifact-backed catalog entry materializes to the cache and then creates an overlay base

- [ ] **Step 2: Run the tests and confirm unsupported artifact failure**

Run:

```bash
uv run python -m pytest \
  tests/providers/local_libvirt/test_materialize.py \
  tests/providers/local_libvirt/test_rootfs_resolve.py \
  -q
```

Expected: FAIL because artifact-backed materialization is still rejected.

- [ ] **Step 3: Implement authorized artifact materialization**

In `materialize_rootfs_base()`, resolve artifact-backed component refs through the component DB
helpers using the job's authorizing project. Download through the object-store client into
`<cache>.part`, verify sha256 and `qemu-img info`, set QEMU-readable mode/label, then atomically
rename to the digest cache path. Keep cache hits authorization-gated: a cache file exists only as a
performance detail and must not make a private component usable by another project.

After artifact-backed rootfs materialization passes, update `ProviderRuntime.component_sources` for
`rootfs` from `frozenset({"local", "catalog"})` to
`frozenset({"local", "artifact", "catalog"})`. Keep kernel, initrd, config, patch, and vmlinux
artifact sources unadvertised until their materializers exist.

- [ ] **Step 4: Run focused tests**

Run the command from Step 2 again.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt tests/providers/local_libvirt
git commit -m "feat: materialize artifact-backed local-libvirt rootfs"
```

---

## Milestone 12: Documentation Replacement And Live Fixture Gate

**Files:**
- Modify: `docs/adr/0053-build-checkout-seam.md`
- Modify: `docs/superpowers/specs/2026-06-06-build-checkout-seam-design.md`
- Modify: `docs/superpowers/specs/2026-06-04-build-plane-design.md`
- Modify: `docs/runbooks/live-stack.md`
- Modify: `tests/integration/test_live_stack.py`

- [ ] **Step 1: Update superseded docs**

Replace prose that says `config_ref` is the profile's complete local `.config` with ADR-0065's
model: agent-supplied complete config plus additive profile requirements. Point readers to
ADR-0065 for the current decision.

- [ ] **Step 2: Replace stale `/configs/kdump.config` assumptions**

Update live-stack tests and runbook setup so fixture profile requirements come from
`fixtures/local-libvirt/configs/*.required.config`, not `/configs/kdump.config`.

- [ ] **Step 3: Run docs/style checks and focused live-stack unit tests**

Run:

```bash
just check-mermaid
uv run python -m pytest tests/integration/live_stack/test_harness_unit.py tests/integration/test_live_stack.py -q
```

Expected: PASS or live-stack tests skip cleanly when the local services are not running.

- [ ] **Step 4: Commit**

```bash
git add docs tests/integration
git commit -m "docs: align build docs with component fixtures"
```

---

## Milestone 13: Verification

- [ ] **Step 1: Run focused component/provider/MCP tests**

Run:

```bash
uv run python -m pytest \
  tests/provider_components \
  tests/provider_components/test_validation.py \
  tests/providers/local_libvirt/test_materialize.py \
  tests/mcp/catalog \
  tests/mcp/providers \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run standard local gates**

Run:

```bash
just lint
just type
just test
```

Expected: PASS with no warnings.

- [ ] **Step 3: Run fixture dry-run**

Run:

```bash
scripts/fixtures/local-libvirt env
scripts/fixtures/local-libvirt verify --dry-run
```

Expected: `env` prints host-process variables; `verify --dry-run` reports host checks and any
operator-missing rootfs image without modifying libvirt.

- [ ] **Step 4: Optional live smoke after operator rootfs is present**

Run:

```bash
scripts/fixtures/local-libvirt prepare
scripts/fixtures/local-libvirt verify --smoke-boot console-ready_x86_64
```

Expected: local-libvirt boots the selected profile to the console-ready marker.

---

## Self-Review

- ADR-0065 coverage: component reference kinds, per-component source support, provider-scoped
  rootfs catalog, local cache/materialization, additive config requirements, provider validation,
  and private linked/uploaded components are each mapped to implementation milestones.
- Spec coverage: fixture bundle, local-libvirt operator helper, MCP discovery tools, local-only
  rootfs links, S3/artifact support boundaries, and current-doc replacement are covered.
- Intentional first-slice limits: artifact-backed materialization is modeled and rejected until the
  DB/component registry and authorized object-store path are wired, then implemented in its own
  milestone. This keeps local-only support deliverable before artifact-backed cache support lands.
- Style scan: this plan uses "Milestone" terminology; it avoids case-file automation and keeps
  `docs/test-cases` as human notes only.
