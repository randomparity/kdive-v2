# Systems Config Consolidation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate every operator-configurable inventory element — images, provider instances, build hosts, and their capacity/cost — into a single declarative `systems.toml`, loaded into the DB by a merge-reconcile engine, with first-class support for multiple instances per provider and an agent-native runtime-mutation path.

**Architecture:** A new `src/kdive/inventory/` package parses `systems.toml` (schema v2) into a typed pydantic `InventoryDoc`, then per-entity reconcilers merge it into `image_catalog` / `resources` / `build_hosts` keyed by stable identity. A `managed_by` column (`config` | `discovery` | `runtime`) partitions ownership so declarative bring-up and imperative agent tools own disjoint row-sets. The engine runs from one place via three triggers: a `kdive reconcile-systems` CLI, a reconciler-loop `reconcile_inventory` repair spec (drift), and an `ops.reconcile_now`-style MCP trigger. Prune is non-destructive (cordon-only, refuse-if-live), mirroring the existing reaper contract.

**Tech Stack:** Python 3.13, pydantic v2 (discriminated unions), `tomllib`, psycopg3 + async pool, FastMCP tools, existing reconciler-loop `_RepairSpec` registry, disposable-Postgres integration tests (ADR-0019).

**Spec:** `docs/superpowers/specs/2026-06-14-systems-config-consolidation-design.md`
**ADR:** `docs/adr/0112-systems-inventory-config.md`
**Delivery:** four independently-shippable, bisectable phases. The Phase-1 migration authors **all** schema this design needs (additive, forward-only, single-migration-owner); later phases only populate/read columns that already exist.

---

## Confirmed code seams (anchors for every task)

| What | Where (verified) |
|---|---|
| Migration dir + next number | `src/kdive/db/schema/NNNN_*.sql`; latest `0029_*`, **next = `0030`** |
| `resources` DDL (`host_uri`, kind CHECK) | `src/kdive/db/schema/0001_init.sql:13` (kind CHECK widened by 0018/0020) |
| `image_catalog` DDL + the CHECK to relax | `src/kdive/db/schema/0023_image_catalog.sql:33` — `image_object_present CHECK ((state='defined') = (object_key IS NULL))` |
| Reconciler repair-spec registry | `src/kdive/reconciler/loop.py` — `_RepairSpec` (l.138), `_build_repairs` (l.199) |
| On-demand MCP reconcile (mirror) | `src/kdive/mcp/tools/ops/reconcile.py` (`ops.reconcile_now`, gated `platform_operator`) |
| Existing ops inventory tool surface | `src/kdive/mcp/tools/ops/inventory.py` |
| `build_hosts.register` (auth/shape mirror) | `src/kdive/mcp/tools/ops/build_hosts/register.py` |
| Fault-inject hardcoded caps (#385) | `src/kdive/providers/fault_inject/discovery.py:90` (`list_resources` capability dict — no `vcpus`/`memory_mb`) |
| Allocation admission seam | `src/kdive/services/allocation/admission.py` — `admit()` (l.160), `_admit_under_project_lock` (l.296) |
| `REMOTE_BASE_IMAGE_NAME` literal | `src/kdive/providers/remote_libvirt/rootfs_build.py:49` |
| Image defs in code (delete targets) | `src/kdive/images/seed_data/` + inline YAML in `src/kdive/admin/default_fixtures.py:6+` |
| Singleton remote env config | `src/kdive/providers/remote_libvirt/{config.py,settings.py,discovery.py}` |
| Existing `systems.toml` consumer | `scripts/coverage_campaign/systems.py` (campaign `render-env`/`setup-commands`) |

---

## File structure

**New package `src/kdive/inventory/`** (one responsibility per file):

- `model.py` — pydantic types: `InventoryDoc`, `ImageEntry`, `ImageSource` (discriminated union `S3Source | BuildSource | StagedSource`), `RemoteLibvirtInstance`, `LocalLibvirtInstance`, `FaultInjectInstance`, `BuildHostInstance`. Parse-time validation only (identity uniqueness, cross-ref, discriminator).
- `loader.py` — `load_inventory(path) -> InventoryDoc`: read file, `tomllib.loads`, `InventoryDoc.model_validate`, raise `InventoryError` naming entry+field on failure.
- `reconcile.py` — engine core: `ReconcileDiff` (created / updated / pruned / cordoned / warned), `ManagedBy` enum, the shared upsert/prune merge contract, the per-identity advisory lock.
- `reconcile_images.py` — `reconcile_images(conn, doc) -> ReconcileDiff`.
- `reconcile_resources.py` — Phase 2: `reconcile_resources(conn, doc, discovered)`.
- `reconcile_build_hosts.py` — Phase 3.
- `errors.py` — `InventoryError(entry, field, msg)`.

**New migration:** `src/kdive/db/schema/0030_systems_inventory.sql` (authors all schema).

**New CLI:** `src/kdive/cli/reconcile_systems.py` (or extend the existing `kdive` CLI entrypoint) — `kdive reconcile-systems`.

**New reconciler spec:** wire `reconcile_inventory` into `src/kdive/reconciler/loop.py:_build_repairs`.

**New MCP tools (Phase 4):** `src/kdive/mcp/tools/ops/resources/{register.py,deregister.py,renew.py}` (mirror `build_hosts/register.py`).

**Tests:** `tests/inventory/` (units), `tests/integration/test_reconcile_inventory.py` (disposable PG), `tests/guards/test_no_inventory_in_code.py` (the delete-guard).

---

# Phase 1 — schema + engine + images

Outcome: zero image definitions remain in code; images load from `systems.toml`. This phase ships the migration (all schema), the parser, the engine core, `reconcile_images`, the CLI, the loop spec, and deletes the in-code image defs.

### Task 1.1: The migration (all schema this design needs)

**Files:**
- Create: `src/kdive/db/schema/0030_systems_inventory.sql`
- Test: `tests/integration/test_migrate.py` (existing migrate-list test — extend its expected count)

- [ ] **Step 1: Write the failing migration-list test update**

In `tests/integration/test_migrate.py`, bump the expected applied-migration count / list to include `0030_systems_inventory`. (The suite asserts the full ordered list; add the new filename as the last entry.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_migrate.py -v`
Expected: FAIL — `0030_systems_inventory` not found / count mismatch.

- [ ] **Step 3: Write the migration**

```sql
-- 0030_systems_inventory.sql — schema for ADR-0112 (systems.toml inventory).
-- Authors ALL schema the four-phase design needs (additive, forward-only, ADR-0015).
-- Later phases only populate/read these columns.

-- managed_by partitions ownership on every reconciled table.
ALTER TABLE image_catalog ADD COLUMN managed_by text NOT NULL DEFAULT 'runtime'
    CONSTRAINT image_catalog_managed_by_check
    CHECK (managed_by IN ('config', 'discovery', 'runtime'));
ALTER TABLE resources ADD COLUMN managed_by text NOT NULL DEFAULT 'runtime'
    CONSTRAINT resources_managed_by_check
    CHECK (managed_by IN ('config', 'discovery', 'runtime'));
ALTER TABLE build_hosts ADD COLUMN managed_by text NOT NULL DEFAULT 'runtime'
    CONSTRAINT build_hosts_managed_by_check
    CHECK (managed_by IN ('config', 'discovery', 'runtime'));

-- Staged images: a registered image may carry a provider volume instead of an S3 object_key.
ALTER TABLE image_catalog ADD COLUMN volume text;
-- Relax the original image_object_present CHECK (0023): a non-'defined' row must have
-- EXACTLY ONE of object_key / volume; a 'defined' row has neither.
ALTER TABLE image_catalog DROP CONSTRAINT image_object_present;
ALTER TABLE image_catalog ADD CONSTRAINT image_object_present CHECK (
    (state = 'defined' AND object_key IS NULL AND volume IS NULL)
    OR (state <> 'defined' AND (object_key IS NULL) <> (volume IS NULL))
);

-- Resource stable identity: a mutable unique name (resource_id UUID stays the PK/FK target).
ALTER TABLE resources ADD COLUMN name text;
CREATE UNIQUE INDEX resources_kind_name_key ON resources (kind, name) WHERE name IS NOT NULL;

-- Per-project affinity: NULL = global (any project). owner_project + allowlist scope a resource.
ALTER TABLE resources ADD COLUMN owner_project text;
ALTER TABLE resources ADD COLUMN affinity_allowlist text[] NOT NULL DEFAULT '{}';

-- Lease for runtime-registered resources (leak reaping). NULL for config/discovery rows.
ALTER TABLE resources ADD COLUMN lease_expires_at timestamptz;

-- Backfill ownership for pre-existing rows (load-bearing, see plan §backfill):
UPDATE resources SET managed_by = 'discovery';        -- discovered hosts: never pruned on first reconcile
UPDATE image_catalog SET managed_by = 'config';       -- seeded baseline is config-equivalent: reconcile owns it
-- affinity already defaults global (owner_project NULL); no allocation regresses.
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_migrate.py -v`
Expected: PASS. Also run `uv run pytest tests/integration/test_migrate.py -v` twice in one session if the harness applies twice (idempotency) — additive DDL is forward-only.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/db/schema/0030_systems_inventory.sql tests/integration/test_migrate.py
git commit -m "feat(inventory): author systems-inventory schema migration (ADR-0112)"
```

**Note for the implementer:** the `managed_by` backfill is the most failure-prone line in the whole effort. `resources → 'discovery'` so the first reconcile does **not** prune a real discovered host that is not yet declared in `systems.toml`. Seeded `image_catalog → 'config'` so reconcile fully owns the catalog (a previously-seeded image the operator did not migrate into the file is then pruned **under the cordon guard**, not stranded as an unowned orphan). Do not collapse these two into one default.

### Task 1.2: The pydantic model + discriminated source union

**Files:**
- Create: `src/kdive/inventory/model.py`, `src/kdive/inventory/errors.py`
- Test: `tests/inventory/test_model.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/inventory/test_model.py
import pytest
from kdive.inventory.model import InventoryDoc
from kdive.inventory.errors import InventoryError


def _doc(**overrides):
    base = {
        "schema_version": 2,
        "image": [{
            "provider": "remote-libvirt", "name": "base", "arch": "x86_64",
            "format": "qcow2", "root_device": "/dev/vda", "visibility": "public",
            "source": {"kind": "staged", "volume": "base.qcow2"},
        }],
        "remote_libvirt": [{
            "name": "h1", "uri": "qemu+tls://h1/system", "gdb_addr": "10.0.0.1",
            "gdbstub_range": "47000:47099", "client_cert_ref": "c.pem",
            "client_key_ref": "k.pem", "ca_cert_ref": "ca.pem",  # pragma: allowlist secret - filename ref
            "base_image": "base", "cost_class": "remote",
            "concurrent_allocation_cap": 1, "shapes": ["small"],
        }],
    }
    base.update(overrides)
    return base


def test_wellformed_parses():
    doc = InventoryDoc.model_validate(_doc())
    assert doc.image[0].source.kind == "staged"
    assert doc.remote_libvirt[0].base_image == "base"


def test_s3_source_requires_digest_field_present():
    d = _doc(image=[{
        "provider": "local-libvirt", "name": "i", "arch": "x86_64",
        "format": "qcow2", "root_device": "/dev/vda", "visibility": "public",
        "source": {"kind": "s3", "object_key": "k", "digest": "sha256:ab"},
    }])
    assert InventoryDoc.model_validate(d).image[0].source.object_key == "k"


def test_duplicate_image_identity_rejected():
    img = {
        "provider": "local-libvirt", "name": "dup", "arch": "x86_64",
        "format": "qcow2", "root_device": "/dev/vda", "visibility": "public",
        "source": {"kind": "staged", "volume": "v.qcow2"},
    }
    with pytest.raises(InventoryError):
        InventoryDoc.model_validate(_doc(image=[img, dict(img)], remote_libvirt=[]))


def test_base_image_cross_ref_must_name_declared_image():
    d = _doc()
    d["remote_libvirt"][0]["base_image"] = "does-not-exist"
    with pytest.raises(InventoryError):
        InventoryDoc.model_validate(d)


def test_unknown_source_kind_rejected():
    d = _doc()
    d["image"][0]["source"] = {"kind": "ftp", "url": "x"}
    with pytest.raises(InventoryError):
        InventoryDoc.model_validate(d)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/inventory/test_model.py -v`
Expected: FAIL — `kdive.inventory.model` does not exist.

- [ ] **Step 3: Implement the model**

```python
# src/kdive/inventory/errors.py
from __future__ import annotations


class InventoryError(ValueError):
    """A systems.toml parse/validation failure, naming the offending entry + field."""

    def __init__(self, entry: str, field: str, msg: str) -> None:
        self.entry = entry
        self.field = field
        super().__init__(f"{entry}.{field}: {msg}")
```

```python
# src/kdive/inventory/model.py
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from kdive.inventory.errors import InventoryError


class S3Source(BaseModel):
    kind: Literal["s3"]
    object_key: str
    digest: str | None = None  # required to reach 'registered'; HEAD only confirms existence


class BuildSource(BaseModel):
    kind: Literal["build"]
    base: str
    components: list[str] = Field(default_factory=list)


class StagedSource(BaseModel):
    kind: Literal["staged"]
    volume: str


ImageSource = Annotated[
    S3Source | BuildSource | StagedSource, Field(discriminator="kind")
]


class ImageEntry(BaseModel):
    provider: str
    name: str
    arch: str
    format: str
    root_device: str
    visibility: Literal["public", "private"]
    capabilities: list[str] = Field(default_factory=list)
    source: ImageSource

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.provider, self.name, self.arch)


class _Instance(BaseModel):
    name: str
    cost_class: str
    concurrent_allocation_cap: int = 1


class RemoteLibvirtInstance(_Instance):
    uri: str
    gdb_addr: str
    gdbstub_range: str
    client_cert_ref: str
    client_key_ref: str
    ca_cert_ref: str
    base_image: str
    shapes: list[str] = Field(default_factory=list)


class LocalLibvirtInstance(_Instance):
    host_uri: str


class FaultInjectInstance(_Instance):
    vcpus: int
    memory_mb: int
    seed: int = 0


class BuildHostInstance(BaseModel):
    name: str
    kind: str
    base_image_volume: str | None = None
    workspace_root: str
    max_concurrent: int = 1


class InventoryDoc(BaseModel):
    schema_version: Literal[2]
    image: list[ImageEntry] = Field(default_factory=list)
    remote_libvirt: list[RemoteLibvirtInstance] = Field(default_factory=list)
    local_libvirt: list[LocalLibvirtInstance] = Field(default_factory=list)
    fault_inject: list[FaultInjectInstance] = Field(default_factory=list)
    build_host: list[BuildHostInstance] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_identities_and_refs(self) -> "InventoryDoc":
        seen: set[tuple[str, str, str]] = set()
        for img in self.image:
            if img.identity in seen:
                raise InventoryError(f"image[{img.name}]", "identity", "duplicate (provider,name,arch)")
            seen.add(img.identity)
        declared = {img.name for img in self.image}
        for inst in self.remote_libvirt:
            if inst.base_image not in declared:
                raise InventoryError(
                    f"remote_libvirt[{inst.name}]", "base_image",
                    f"names undeclared image {inst.base_image!r}",
                )
        for group in (self.remote_libvirt, self.local_libvirt, self.fault_inject, self.build_host):
            names = [i.name for i in group]
            dupes = {n for n in names if names.count(n) > 1}
            if dupes:
                raise InventoryError("instance", "name", f"duplicate instance names {sorted(dupes)}")
        return self
```

**Note:** pydantic raises `ValidationError` for a bad discriminator before the model-validator runs. Convert it at the loader boundary (Task 1.3) so callers always see `InventoryError`; the `test_unknown_source_kind_rejected` test asserts `InventoryError`, so make `InventoryDoc.model_validate` calls go through the loader in tests that need conversion — OR add a thin classmethod `InventoryDoc.parse(data)` that wraps `model_validate` and re-raises `ValidationError` as `InventoryError`. Use the classmethod and point the tests at `InventoryDoc.parse` instead of `model_validate` for the discriminator case.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/inventory/test_model.py -v`
Expected: PASS (after switching the discriminator-failure test to `InventoryDoc.parse`).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/inventory/model.py src/kdive/inventory/errors.py tests/inventory/test_model.py
git commit -m "feat(inventory): typed systems.toml v2 model + discriminated source union"
```

### Task 1.3: The loader

**Files:**
- Create: `src/kdive/inventory/loader.py`
- Test: `tests/inventory/test_loader.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/inventory/test_loader.py
import pytest
from kdive.inventory.loader import load_inventory
from kdive.inventory.errors import InventoryError

GOOD = """
schema_version = 2
[[image]]
provider = "remote-libvirt"
name = "base"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "base.qcow2"
"""

BAD_TOML = "schema_version = 2\n[[image]\n"  # malformed


def test_load_good(tmp_path):
    p = tmp_path / "systems.toml"
    p.write_text(GOOD)
    doc = load_inventory(p)
    assert doc.image[0].name == "base"


def test_malformed_toml_raises_inventory_error(tmp_path):
    p = tmp_path / "systems.toml"
    p.write_text(BAD_TOML)
    with pytest.raises(InventoryError):
        load_inventory(p)


def test_missing_file_raises_inventory_error(tmp_path):
    with pytest.raises(InventoryError):
        load_inventory(tmp_path / "absent.toml")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/inventory/test_loader.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the loader**

```python
# src/kdive/inventory/loader.py
from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import ValidationError

from kdive.inventory.errors import InventoryError
from kdive.inventory.model import InventoryDoc


def load_inventory(path: Path) -> InventoryDoc:
    """Read + parse + validate systems.toml into a typed InventoryDoc.

    Raises:
        InventoryError: file missing, malformed TOML, or schema validation failure —
            always this type, so callers fault-isolate one exception.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InventoryError(str(path), "file", f"cannot read: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise InventoryError(str(path), "toml", f"malformed: {exc}") from exc
    try:
        return InventoryDoc.parse(data)
    except ValidationError as exc:
        raise InventoryError(str(path), "schema", str(exc)) from exc
```

Add `InventoryDoc.parse` classmethod in `model.py`:

```python
    @classmethod
    def parse(cls, data: dict) -> "InventoryDoc":
        from pydantic import ValidationError
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise InventoryError("inventory", "schema", str(exc)) from exc
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/inventory/test_loader.py tests/inventory/test_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/inventory/loader.py src/kdive/inventory/model.py tests/inventory/test_loader.py
git commit -m "feat(inventory): systems.toml loader with InventoryError boundary"
```

### Task 1.4: Engine core + `reconcile_images` (the load-bearing merge contract)

**Files:**
- Create: `src/kdive/inventory/reconcile.py`, `src/kdive/inventory/reconcile_images.py`
- Test: `tests/integration/test_reconcile_inventory.py` (disposable PG, ADR-0019)

This is the heart of the design. The integration test encodes the spec's invariants 1–3, 5, 7, 8 for images (resource invariants 4, 6 land in Phases 2 and 4).

- [ ] **Step 1: Write the failing integration tests** (one per invariant)

```python
# tests/integration/test_reconcile_inventory.py
import pytest
from kdive.inventory.loader import load_inventory
from kdive.inventory.reconcile_images import reconcile_images

# Fixtures `pg_conn` (disposable migrated DB) and `write_toml(tmp_path, body)` assumed
# per the existing integration harness; mirror tests/integration/test_migrate.py setup.


async def test_staged_image_registers_with_volume(pg_conn, write_toml):
    doc = load_inventory(write_toml("""
        schema_version = 2
        [[image]]
        provider = "remote-libvirt"
        name = "base"; arch = "x86_64"; format = "qcow2"
        root_device = "/dev/vda"; visibility = "public"
        [image.source]
        kind = "staged"
        volume = "base.qcow2"
    """))
    diff = await reconcile_images(pg_conn, doc)
    row = await _one(pg_conn, "base")
    assert row["state"] == "registered"
    assert row["volume"] == "base.qcow2" and row["object_key"] is None
    assert row["managed_by"] == "config"
    assert "base" in {c.name for c in diff.created}


async def test_s3_image_without_digest_stays_defined(pg_conn, write_toml):
    doc = load_inventory(write_toml("""
        schema_version = 2
        [[image]]
        provider = "local-libvirt"
        name = "i"; arch = "x86_64"; format = "qcow2"
        root_device = "/dev/vda"; visibility = "public"
        [image.source]
        kind = "s3"
        object_key = "rootfs/i.qcow2"
    """))   # no digest, object assumed absent in fake store
    diff = await reconcile_images(pg_conn, doc)
    row = await _one(pg_conn, "i")
    assert row["state"] == "defined"
    assert any("i" in w.entry for w in diff.warned)


async def test_reconcile_never_overwrites_realized_object_key(pg_conn, write_toml):
    # Seed a build-realized row (state=registered, object_key set, managed_by=config).
    await _insert_registered_build_row(pg_conn, name="built", object_key="rootfs/built.qcow2",
                                       digest="sha256:dead")
    doc = load_inventory(write_toml("""
        schema_version = 2
        [[image]]
        provider = "local-libvirt"
        name = "built"; arch = "x86_64"; format = "qcow2"
        root_device = "/dev/vda"; visibility = "public"
        [image.source]
        kind = "build"
        base = "fedora-43"
    """))
    await reconcile_images(pg_conn, doc)
    row = await _one(pg_conn, "built")
    assert row["state"] == "registered"          # NOT downgraded to defined
    assert row["object_key"] == "rootfs/built.qcow2" and row["digest"] == "sha256:dead"


async def test_prune_removes_only_config_rows_absent_from_config(pg_conn, write_toml):
    await _insert_registered_build_row(pg_conn, name="runtime-img", object_key="k",
                                       digest="sha256:1", managed_by="runtime")
    await _insert_config_staged_row(pg_conn, name="stale-config", volume="v.qcow2")
    doc = load_inventory(write_toml("schema_version = 2\n"))  # empty: nothing declared
    diff = await reconcile_images(pg_conn, doc)
    assert await _exists(pg_conn, "runtime-img")             # runtime row untouched
    assert not await _exists(pg_conn, "stale-config")        # config row pruned (idle)
    assert "stale-config" in {p.name for p in diff.pruned}


async def test_prune_of_in_use_image_cordons_not_deletes(pg_conn, write_toml):
    await _insert_config_staged_row(pg_conn, name="busy", volume="v.qcow2")
    await _attach_dependent_system(pg_conn, image_name="busy")   # live dependent
    doc = load_inventory(write_toml("schema_version = 2\n"))
    diff = await reconcile_images(pg_conn, doc)
    assert await _exists(pg_conn, "busy")                    # NOT deleted
    assert "busy" in {c.name for c in diff.cordoned}


async def test_reconcile_is_idempotent(pg_conn, write_toml):
    body = """
        schema_version = 2
        [[image]]
        provider = "remote-libvirt"
        name = "base"; arch = "x86_64"; format = "qcow2"
        root_device = "/dev/vda"; visibility = "public"
        [image.source]
        kind = "staged"
        volume = "base.qcow2"
    """
    doc = load_inventory(write_toml(body))
    await reconcile_images(pg_conn, doc)
    diff2 = await reconcile_images(pg_conn, doc)
    assert not diff2.created and not diff2.updated and not diff2.pruned
```

(Provide the `_one`/`_exists`/`_insert_*`/`_attach_dependent_system` helpers in the test module — direct SQL against `pg_conn`. The dependent-system link uses whatever `system → image` reference Task 1.5 establishes; if that reference does not yet exist, this test degrades to "refuse prune of any `registered` image" per the spec's safe-degradation clause — assert on that weaker guard and leave a `# TODO(phase-1.5): tighten once system→image ref lands` only if 1.5 is sequenced after this; otherwise sequence 1.5 first.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/integration/test_reconcile_inventory.py -v`
Expected: FAIL — `reconcile_images` missing.

- [ ] **Step 3: Implement engine core + `reconcile_images`**

`reconcile.py` provides: `ManagedBy` enum; `ReconcileDiff` dataclass (`created`, `updated`, `pruned`, `cordoned`, `warned` lists of small records); a `per_identity_lock(conn, key)` helper using `advisory_xact_lock` (reuse `kdive.store...advisory_xact_lock`); and a `_prune_or_cordon(conn, row, is_live)` helper encoding the non-destructive contract.

`reconcile_images.py`:
- Load existing `image_catalog` rows.
- For each config `ImageEntry`: upsert keyed by `(provider, name, arch)`, writing only config-owned fields (`provider/name/arch/format/root_device/visibility/capabilities/managed_by='config'`), **never** `object_key`/`digest`/`state` when the existing row is already `registered` from a build (invariant 1).
  - `staged` → set `volume`, `state='registered'` (no S3).
  - `s3` → HEAD the object (existence). If digest supplied (config) → `state='registered'`, set `object_key`+`digest`; else leave `state='defined'` + append a `warned` entry (invariant 8). Missing object → stay `defined` + warn (degrade cleanly).
  - `build` → ensure a `defined` row exists; never downgrade a realized row (invariant 1). If the row's `base_image` is referenced but not yet `registered`, append a `warned`/degraded marker.
- Prune: for each existing `managed_by='config'` row whose `(provider,name,arch)` is absent from config, call `_prune_or_cordon` — delete if idle, cordon + `diff.cordoned` if it has dependent systems (live per ADR-0109 non-terminal predicate). Never delete S3 bytes inline (GC owns that). Runtime/discovery rows: skip (invariant 2/3).
- All image work in **one transaction** (all-or-nothing per entity type).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/integration/test_reconcile_inventory.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/inventory/reconcile.py src/kdive/inventory/reconcile_images.py tests/integration/test_reconcile_inventory.py
git commit -m "feat(inventory): reconcile engine core + reconcile_images merge contract"
```

### Task 1.5: Establish the `system → image` reference (prune-guard prerequisite)

**Files:**
- Modify: whichever table records a System's image (verify: `rg -n "image" src/kdive/db/schema/0001_init.sql` and the systems/allocations DDL). If a FK/reference already exists, this task is a no-op confirmation + a test; if absent, add the column in the **Task 1.1 migration** (fold it in — do not add a second migration) and a backfill.
- Test: `tests/integration/test_reconcile_inventory.py::test_prune_of_in_use_image_cordons_not_deletes` (already written in 1.4).

- [ ] **Step 1:** Determine whether a System row already references its source image. Run `rg -n "image_id|image_name|rootfs|source_image" src/kdive/db/schema/*.sql`.
- [ ] **Step 2:** If absent, add `systems.image_id uuid REFERENCES image_catalog(...)` (nullable) to `0030_systems_inventory.sql` and have the live path populate it. If present, wire the prune-guard query to it.
- [ ] **Step 3:** Run the cordon test (1.4) and confirm it asserts the **tight** guard (dependent-system resolved), not the degraded fallback.
- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat(inventory): resolve system->image reference for non-destructive image prune"
```

(If the reference genuinely already exists, fold this into Task 1.4 and skip the separate commit.)

### Task 1.6: `kdive reconcile-systems` CLI + loop spec `reconcile_inventory`

**Files:**
- Create: `src/kdive/cli/reconcile_systems.py` (or a subcommand on the existing CLI)
- Modify: `src/kdive/reconciler/loop.py:_build_repairs` (add `_RepairSpec("reconcile_inventory", _reconcile_inventory_pass)`)
- Test: `tests/integration/test_reconcile_inventory.py` (loop fault-isolation), `tests/inventory/test_cli.py`

- [ ] **Step 1: Write the failing fault-isolation test**

```python
async def test_loop_inventory_pass_is_fault_isolated(pg_conn, monkeypatch, tmp_path):
    # A malformed systems.toml must NOT abort sibling reaper repairs.
    bad = tmp_path / "systems.toml"
    bad.write_text("schema_version = 2\n[[image]\n")  # malformed
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(bad))
    report = await reconcile_once(pg_conn, _config_with_inventory_spec())
    assert "reconcile_inventory" in report.failures        # this pass failed
    assert report.counts["reaped_active_allocations"] >= 0 # siblings still ran
```

- [ ] **Step 2: Run to verify it fails.** `uv run pytest tests/integration/test_reconcile_inventory.py -k fault_isolated -v` → FAIL.

- [ ] **Step 3: Implement.**
  - `_reconcile_inventory_pass(conn)`: read `KDIVE_SYSTEMS_TOML` (default `./systems.toml`), `load_inventory`, `reconcile_images` (Phase 1). Catch `InventoryError`, log + re-raise so the existing `_build_repairs` try/except records it as a failed-this-pass spec (sibling repairs already keep running — that's the existing loop contract at `loop.py:350-356`). Content-hash gate (skip if file unchanged since last pass).
  - Add the spec to `_build_repairs`. **Do not** let an inventory failure raise out of `reconcile_once`.
  - CLI: `kdive reconcile-systems [--path P]` → run `reconcile_images` once against the pool, print the `ReconcileDiff`, exit non-zero on `InventoryError`.

- [ ] **Step 4: Run to verify it passes.** `uv run pytest tests/integration/test_reconcile_inventory.py tests/inventory -v` → PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/cli/reconcile_systems.py src/kdive/reconciler/loop.py tests/inventory/test_cli.py tests/integration/test_reconcile_inventory.py
git commit -m "feat(inventory): kdive reconcile-systems CLI + fault-isolated reconcile_inventory loop pass"
```

### Task 1.7: Delete in-code image definitions + guard test

**Files:**
- Delete: `src/kdive/images/seed_data/` (whole tree), the inline rootfs YAML block in `src/kdive/admin/default_fixtures.py`, the `REMOTE_BASE_IMAGE_NAME` literal (`src/kdive/providers/remote_libvirt/rootfs_build.py:49`) — replace the literal's single consumer with a `base_image`-from-config lookup.
- Create: `systems.toml.example` image entries (the baseline images that were in `seed_data`), and add the same to the real (gitignored) `systems.toml`.
- Create: `tests/guards/test_no_inventory_in_code.py`
- Test: full suite (these are delete-and-rewire changes).

- [ ] **Step 1: Write the failing guard test**

```python
# tests/guards/test_no_inventory_in_code.py
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "kdive"


def test_no_seed_data_tree():
    assert not (SRC / "images" / "seed_data").exists()


def test_no_inline_rootfs_yaml_in_fixtures():
    text = (SRC / "admin" / "default_fixtures.py").read_text()
    assert "rootfs/fedora-kdive-ready" not in text
    assert "schema_version: 1" not in text  # the embedded manifest YAML


def test_no_remote_base_image_literal():
    text = (SRC / "providers" / "remote_libvirt" / "rootfs_build.py").read_text()
    assert "fedora-kdive-remote-base-43" not in text
    assert "REMOTE_BASE_IMAGE_NAME" not in text
```

(The `KDIVE_REMOTE_LIBVIRT_*` singleton assertion is added in **Phase 3** — its removal is Phase 3's job. Leave a comment to that effect.)

- [ ] **Step 2: Run to verify it fails.** `uv run pytest tests/guards/test_no_inventory_in_code.py -v` → FAIL (files still present).

- [ ] **Step 3: Delete + rewire.**
  - `trash src/kdive/images/seed_data` (recoverable, never `rm -rf`).
  - Remove the inline YAML strings from `default_fixtures.py`; whatever consumed them now relies on the reconciled catalog.
  - Replace `REMOTE_BASE_IMAGE_NAME` usage with a lookup of the remote instance's `base_image` config field (Phase 3 fully removes the singleton path; for Phase 1, resolve the base-image name from the reconciled `image_catalog` staged row).
  - Add the baseline images to `systems.toml.example` (and real `systems.toml`).
  - Fix every import/consumer the deletion breaks (the build/seed path that loaded `seed_data`).

- [ ] **Step 4: Run to verify it passes + full suite green.** `uv run pytest tests/guards -v && just test` → PASS, zero warnings.

- [ ] **Step 5: Commit.**

```bash
git add -A
git commit -m "feat(inventory): remove in-code image defs; load images from systems.toml (closes the Phase-1 outcome)"
```

**Phase-1 done check:** `just lint && just type && just test` all green; guard test passes; a fresh `kdive reconcile-systems` against an empty DB + the baseline `systems.toml` produces the same image rows the old `seed_data` produced.

---

# Phase 2 — resources: capacity, cost, merge-with-discovery (fixes #385)

Outcome: `allocations.request(kind=fault-inject)` works; cost/capacity declared in config, not hardcoded. Sub-issue-level specs (this phase gets its own detailed bite-sized plan at execution time; the invariants below are the acceptance contract).

### Task 2.1: `reconcile_resources` config-overlay
- **Files:** create `src/kdive/inventory/reconcile_resources.py`; tests in `tests/integration/test_reconcile_inventory.py`.
- **Contract:** `managed_by` governs existence (`config` for declared `fault_inject`/`remote_libvirt` instances; `discovery` for host-probed `local-libvirt`); a **config overlay** applies declared attributes (`cost_class`, caps, `vcpus`/`memory_mb`) onto the resource's `capabilities` jsonb keyed by instance `name`, regardless of who created the row.
- **Identity:** key by `(kind, name)` (the migration's unique index); `resource_id` UUID stays the PK/FK target.
- **Discovery bind:** a discovered row binds to its config instance by `host_uri` and inherits that instance's `name`; a discovered host with no config instance gets a deterministic `name` derived from `host_uri`.
- **Test (invariant 4, the #385 regression):** after reconcile, the fault-inject resource's `capabilities` carries `vcpus`/`memory_mb` from config, and `allocations.request(kind=fault-inject)` is **admitted**, not `configuration_error`.
- **Test (invariants 1–3, 5):** the image invariants, re-asserted for resources — no overwrite of discovery-owned PCIe/real-vcpu fields; prune only `config` rows; cordon-not-delete a resource with live allocations.

### Task 2.2: Remove fault-inject hardcoded caps
- **Files:** `src/kdive/providers/fault_inject/discovery.py:90` — delete the hardcoded capability dict's missing-caps story; the resource's `vcpus`/`memory_mb` now come from the config overlay (Task 2.1). Keep discovery emitting the row's existence; the overlay supplies sizing.
- **Test:** a unit asserting discovery no longer hardcodes caps and the overlay path supplies them.

### Task 2.3: Wire `local-libvirt` as discovery + config overlay
- `local-libvirt` exists from discovery (real hardware) and **receives** a config overlay (cost/cap) without its discovery-owned vcpus/memory/PCIe being overwritten.

**Phase-2 done check:** #385 closed (fault-inject allocatable over MCP); cost/cap declared; no hardcoded fault-inject caps remain; `just test` green.

---

# Phase 3 — multi-instance + build hosts

Outcome: multiple instances per provider; the last hardcoded host config gone.

### Task 3.1: Array-of-tables multi-instance
- `[[remote_libvirt]]` / `[[local_libvirt]]` / `[[fault_inject]]` already parse as lists (Phase 1 model). Add the reconcile-side support for **N rows per kind** — multiple `[[fault_inject]]` sharing `host_uri = fault-inject://local` coexist via the `(kind, name)` unique index. Selection: by `resource_id`, or any-available by `kind` (no allocation-API change).
- **Test:** two `[[fault_inject]]` instances reconcile to two distinct resource rows, both independently allocatable.

### Task 3.2: `reconcile_build_hosts`
- **Files:** create `src/kdive/inventory/reconcile_build_hosts.py`; reconcile `[[build_host]]` into the `build_hosts` table (`0027`/`0029`), carrying `base_image_volume`, `workspace_root`, `max_concurrent`, `managed_by='config'`.
- **Test:** a `[[build_host]]` reconciles to a `build_hosts` row; prune/cordon contract holds.

### Task 3.3: Delete the singleton remote env vars
- **Delete:** `KDIVE_REMOTE_LIBVIRT_{URI,CLIENT_CERT_REF,CLIENT_KEY_REF,GDB_ADDR,BASE_IMAGE}` reads in `src/kdive/providers/remote_libvirt/{config.py,settings.py,discovery.py}` and their consumers; the remote provider now resolves its connection from the reconciled `resources` row (per instance). Delete `scripts/coverage_campaign/d1.env.template`.
- **Extend the guard test** (`tests/guards/test_no_inventory_in_code.py`): assert no `KDIVE_REMOTE_LIBVIRT_*` singleton env reads remain in `src/kdive/providers/remote_libvirt/`.
- **Note:** several remote lifecycle modules (`install.py`, `provisioning.py`, `build_vm.py`) read `KDIVE_REMOTE_LIBVIRT_*` "per op" (ADR-0076). Each must be rewired to take the instance's config from the resolved resource row — this is the largest mechanical change in Phase 3; budget it as its own sub-issue.

**Phase-3 done check:** multiple instances per provider expressible + allocatable; guard test rejects `KDIVE_REMOTE_LIBVIRT_*`; `just test` green.

---

# Phase 4 — runtime inventory mutation (agent-native)

Outcome: an agent can add/remove a system live, scoped to its project, with leaked additions auto-reaped. Built on the Phase-1 migration columns (`affinity`/`owner_project`/`affinity_allowlist`/`lease_expires_at`).

### Task 4.1: `resources.register` / `deregister` / `renew` tools
- **Files:** create `src/kdive/mcp/tools/ops/resources/{register.py,deregister.py,renew.py}`, mirroring `src/kdive/mcp/tools/ops/build_hosts/register.py`.
- **Auth:** all `platform_admin`, mutating (adding shared capacity). `deregister` of a resource with **live allocations** is **destructive-tier** (platform_admin + typed confirmation / `--force`), like `ops.force_teardown`.
- **register:** same fields as a `[[remote_libvirt]]`/`[[local_libvirt]]`/`[[fault_inject]]` block; **preflight** (reachability probe, secret refs resolve, `base_image` is `registered`); write a `managed_by='runtime'` row; default `owner_project` to the **registering project**. Reject a `name` that already exists as a `config` row.
- **deregister:** operate **only** on `runtime` rows; reject a `config`-owned instance (config is removed by editing the file).
- **renew:** keyed to `resource_id` (not the registering session — survives agent handoff); extends `lease_expires_at`.
- **Test:** register→allocate→renew→deregister round-trip; config-row deregister rejected; live-allocation deregister requires `--force`.

### Task 4.2: Per-project affinity + admission check
- **Files:** `src/kdive/services/allocation/admission.py` — add an affinity check in `admit()`/`_admit_under_project_lock` (l.160/296): a project may place only on a **global** resource (`owner_project IS NULL`) or one it owns / is on the `affinity_allowlist` of.
- **Default-global no-op:** every pre-existing discovered resource and config-declared instance has `owner_project NULL` (Phase-1 backfill), so the check is a strict no-op for current behavior — **no allocation that works today regresses.**
- **Test:** a scoped runtime resource rejects a foreign project; a global resource admits any project; an allowlisted project is admitted; the regression test that all current (global) allocations still pass.

### Task 4.3: Lease + reachability reaping (reconciler reap spec)
- **Files:** new reconciler `_RepairSpec("reap_runtime_resources", ...)` in `loop.py`; reuse the #359 reachability probe.
- **Contract:** reap a `managed_by='runtime'` resource on **lease expiry OR sustained unreachability past TTL** — but **cordon-only / refuse-if-live, never auto-drain** (identical to the config-prune contract; preserves a `crashed` System under live crash-debug per ADR-0109). Config/discovery rows carry no lease and are never lease-reaped.
- **Test (invariant 6 + reap):** a runtime resource past its lease with no live allocation is reaped; one with a live allocation is cordoned + surfaced, not destroyed.

### Task 4.4: Adopt-on-collision in the reconcile engine
- **Files:** `src/kdive/inventory/reconcile_resources.py`.
- **Contract:** a config identity (`name`) matching a `runtime` row → adopt: flip `managed_by → config`, **clear `lease_expires_at`**, **take the config-declared affinity** (default global). Registration + reconcile **serialize on the identity** (advisory lock on `name`) so prune cannot race re-`register`.
- **Test (invariant 6):** adoption clears the lease and applies config affinity (widening a previously project-scoped runtime resource to global unless the file declares a scope).

### Task 4.5: `ops.reconcile_now` gating for inventory prune
- The existing `ops.reconcile_now` is gated `platform_operator` (`mcp/tools/ops/reconcile.py`). Because the inventory pass can **prune**, an inventory-triggering MCP path must be `platform_admin`. Either add a dedicated `ops.reconcile_systems` tool (platform_admin) or gate the inventory pass within `reconcile_now` behind a platform_admin check. **Decide at execution; default: a dedicated `ops.reconcile_systems` tool** (keeps `reconcile_now`'s existing contract untouched).

**Phase-4 done check:** agent register→use→handoff-renew→deregister works; leaked runtime resource auto-reaped (cordon-if-live); affinity no-op proven for existing allocations; `just test` green.

---

## Epic / sub-issue decomposition (drives the coding)

One **epic** + sub-issues, one milestone. Sub-issues are sized for parallel `/work-issue` worktree agents. **Pre-assign disjoint ADR/migration numbers in the dispatch prompt** (lesson: parallel agents otherwise all grab "next free" — see `preassign-adr-migration-numbers` memory). Only the Phase-1 migration `0030` exists; later phases author **no new migrations** (they populate Phase-1 columns) — make that explicit in each sub-issue to prevent migration-number collisions.

| # | Sub-issue | Phase | Depends on | Files (primary) |
|---|---|---|---|---|
| A | Migration `0030` + `system→image` ref | 1 | — | `db/schema/0030_*.sql`, `test_migrate.py` |
| B | Inventory model + loader | 1 | — (parallel with A) | `inventory/{model,loader,errors}.py` |
| C | Engine core + `reconcile_images` | 1 | A, B | `inventory/{reconcile,reconcile_images}.py` |
| D | CLI + `reconcile_inventory` loop pass | 1 | C | `cli/reconcile_systems.py`, `reconciler/loop.py` |
| E | Delete in-code image defs + guard test | 1 | C, D | `images/seed_data/` (del), `default_fixtures.py`, `rootfs_build.py`, `tests/guards/` |
| F | `reconcile_resources` overlay + #385 fix | 2 | C, E | `inventory/reconcile_resources.py`, `fault_inject/discovery.py` |
| G | Multi-instance + `reconcile_build_hosts` | 3 | F | `inventory/reconcile_build_hosts.py` |
| H | Delete `KDIVE_REMOTE_LIBVIRT_*` singletons + rewire remote lifecycle | 3 | G | `providers/remote_libvirt/*`, guard test |
| I | `resources.register/deregister/renew` tools | 4 | F | `mcp/tools/ops/resources/*` |
| J | Affinity admission check | 4 | A | `services/allocation/admission.py` |
| K | Lease + reachability reaping spec | 4 | A, I | `reconciler/loop.py` |
| L | Adopt-on-collision + `ops.reconcile_systems` gating | 4 | C, I, J | `inventory/reconcile_resources.py`, `mcp/tools/ops/` |

**Wave plan:** Wave 1 = {A, B} parallel. Wave 2 = {C}. Wave 3 = {D, E}. Phase-1 ships. Wave 4 = {F}. Wave 5 = {G}, then {H}. Phase-2/3 ship. Wave 6 = {I, J} parallel, then {K, L}. Phase-4 ships.

**Recurring rebase zones** (serialize merges here, per prior orchestration lessons): `reconciler/loop.py` (D, K), `mcp/tools/ops/` registry + generated tool docs (I, L), `services/allocation/admission.py` (F overlay vs J affinity), `tests/integration/test_migrate.py` (A only — no later migrations).

---

## Self-review

- **Spec coverage:** three ownership layers → Tasks 1.1 (managed_by) + 2.1 (overlay) + 4.2 (affinity). Operating model (declarative/imperative disjoint) → 1.1 backfill + 4.1 runtime rows. Adopt-on-collision → 4.4. Three reload triggers → 1.6 (CLI + loop) + 4.5 (MCP). Engine parser/validator → 1.2/1.3. Per-entity reconcilers → 1.4/2.1/3.2. Existence-vs-overlay split → 2.1. Image realization (s3/build/staged + CHECK relax) → 1.1 + 1.4. Runtime mutation (register/deregister/renew + auth) → 4.1. Affinity → 4.2. Lease/reaping → 4.3. Schema v2 → 1.2. All four phases → Phases 1–4. Deletes → 1.7 + 2.2 + 3.3. Error handling (fault-isolation) → 1.6. All 8 test invariants → 1.4 (1,2,3,5,7,8), 2.1 (4), 4.3/4.4 (6). Guard test → 1.7 + 3.3. Every spec section maps to a task.
- **Placeholder scan:** Phase 1 carries complete code (migration SQL, model, loader, test bodies). Phases 2–4 are sub-issue specs with concrete file paths + test invariants, each getting its own bite-sized plan at execution (per the spec's "four phases, each its own implementation plan"). No "TBD"/"add validation"/"handle edge cases" left unspecified.
- **Type consistency:** `InventoryDoc`/`ImageEntry`/`ImageSource`/`*Instance` names match across model, loader, and reconcile tasks. `ReconcileDiff` fields (`created/updated/pruned/cordoned/warned`) are used consistently in 1.4's tests and 1.6/2.1. `managed_by` values (`config/discovery/runtime`) match the migration CHECK. `(provider,name,arch)` for images vs `(kind,name)` for resources is consistent throughout.
