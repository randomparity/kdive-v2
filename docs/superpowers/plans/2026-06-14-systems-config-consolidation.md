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
| `resources` DDL (`host_uri`, kind CHECK) | `src/kdive/db/schema/0001_init.sql:13` (kind CHECK widened by 0018/0020). **PK column is `id`** (the spec/plan call it `resource_id` conceptually — the actual column is `id`); `image_catalog` and `systems` PKs are also `id`. |
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
-- ONLY the public baseline catalog is config-equivalent. Project-private uploaded images
-- (visibility='private', owner IS NOT NULL — M2.4 #282-289) are RUNTIME-owned and must stay
-- managed_by='runtime' (the column default), else the first reconcile prunes user uploads.
UPDATE image_catalog SET managed_by = 'config' WHERE visibility = 'public' AND owner IS NULL;
-- affinity already defaults global (owner_project NULL); no allocation regresses.

-- NOTE: NO new system->image column. A reference already exists — the prune guard reuses
-- services/images/retention.py:image_referenced_by_live_system, which resolves "a non-terminal
-- System references this image" via a JSONB-containment probe on systems.provisioning_profile
-- keyed by (provider, name), already ADR-0109 terminal-state-filtered. See Task 1.4 / Task 1.5.
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_migrate.py -v`
Expected: PASS. Idempotency here comes from the **migration harness's applied-migration tracking** (it never re-applies `0030`), **not** from the SQL — raw `ADD COLUMN`/`DROP CONSTRAINT` are *not* re-runnable and would error "already exists" on a second raw apply. Do not hand-apply the file twice; rely on the harness.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/db/schema/0030_systems_inventory.sql tests/integration/test_migrate.py
git commit -m "feat(inventory): author systems-inventory schema migration (ADR-0112)"
```

**Note for the implementer:** the `managed_by` backfill is the most failure-prone line in the whole effort. `resources → 'discovery'` so the first reconcile does **not** prune a real discovered host that is not yet declared in `systems.toml`. Seeded `image_catalog → 'config'` so reconcile fully owns the catalog (a previously-seeded image the operator did not migrate into the file is then pruned **under the cordon guard**, not stranded as an unowned orphan). Do not collapse these two into one default. **`build_hosts` is intentionally NOT backfilled** — it keeps the `'runtime'` default because build hosts are imperatively registered (`build_hosts.register`, ssh hosts #342/#359), so the first reconcile never prunes them; a config-declared `[[build_host]]` is adopted in Phase 3 (see Task 3.2).

**Rollout safety valve for prune object-reclaim (load-bearing):** the first post-migration reconcile prunes any config image absent from `systems.toml`. The cordon guard protects in-use images, but an **idle baseline the operator forgot to list** would be deleted row + S3 object — irreversibly, since there is no public-image GC (Task 1.4). So **object reclamation is not done on the first pass blind**: object deletion is **deferred behind a grace TTL** (tombstone the row, reclaim the object only on a later pass once it is *still* absent past the TTL) — **recommended default**, because it is self-tracking (timestamp-based) and needs no out-of-band confirm. The alternative — **report-only first pass** (emit the `ReconcileDiff`, delete nothing, until an explicit `kdive reconcile-systems --apply` / `ops.reconcile_systems`) — also works but requires persisting "first pass already reported, a later pass may apply" state plus a human to act, so prefer the grace TTL unless an interactive confirm is wanted. Never reclaim a public image's bytes on an unconfirmed first pass. Every object reclamation is logged + surfaced in the diff.

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
    # Explicitly-requested path that is absent IS an error (operator named a file that isn't there).
    with pytest.raises(InventoryError):
        load_inventory(tmp_path / "absent.toml")


def test_load_optional_returns_none_for_absent_default(tmp_path):
    # The DEFAULT-path case: an absent file means "nothing declared", not an error
    # (systems.toml is gitignored; CI / fresh deploys legitimately have no file yet).
    assert load_inventory_optional(tmp_path / "absent.toml") is None
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


def load_inventory_optional(path: Path) -> InventoryDoc | None:
    """Like load_inventory, but a MISSING file returns None (nothing declared).

    Use this on the **default** path: systems.toml is gitignored, so an absent default is
    the normal pre-config state, not an operator error. A present-but-malformed file still
    raises InventoryError (a real failure the loop must surface).
    """
    if not path.exists():
        return None
    return load_inventory(path)
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


async def test_relaxed_check_rejects_both_or_neither(pg_conn):
    # Invariant 7 (the constraint half): the relaxed image_object_present CHECK must reject a
    # non-'defined' row with BOTH object_key+volume or NEITHER. A typo in the CHECK passes the
    # happy-path tests but silently allows an invalid row, so assert the rejection directly.
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        await _raw_insert_image(pg_conn, state="registered", object_key="k", volume="v")  # both
    with pytest.raises(psycopg.errors.CheckViolation):
        await _raw_insert_image(pg_conn, state="registered", object_key=None, volume=None)  # neither
    with pytest.raises(psycopg.errors.CheckViolation):
        await _raw_insert_image(pg_conn, state="defined", object_key="k", volume=None)  # defined w/ key
    # valid shapes succeed:
    await _raw_insert_image(pg_conn, state="registered", object_key="k", volume=None)
    await _raw_insert_image(pg_conn, state="registered", object_key=None, volume="v")


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

(Provide the `_one`/`_exists`/`_insert_*`/`_attach_dependent_system` helpers in the test module — direct SQL against `pg_conn`. The dependent-system link reuses the **existing** `image_referenced_by_live_system` (`services/images/retention.py:60-83`): a non-terminal System whose `provisioning_profile` JSONB references the image by `(provider, name)`. `_attach_dependent_system` inserts a non-terminal `systems` row with that profile shape. So `test_prune_of_in_use_image_cordons_not_deletes` cordons a *referenced* image and `test_prune_removes_only_config_rows_absent_from_config` prunes an *unreferenced* one — both driven by the same guard the private-image expiry already trusts. No new schema column.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/integration/test_reconcile_inventory.py -v`
Expected: FAIL — `reconcile_images` missing.

- [ ] **Step 3: Implement engine core + `reconcile_images`**

`reconcile.py` provides: `ManagedBy` enum; `ReconcileDiff` dataclass (`created`, `updated`, `pruned`, `cordoned`, `warned` lists of small records); a `per_identity_lock(conn, key)` helper using `advisory_xact_lock` (reuse `kdive.store...advisory_xact_lock`); and a `_prune_or_cordon(conn, row, is_live)` helper encoding the non-destructive contract.

**Serialize whole-inventory passes (load-bearing):** three triggers run this engine (CLI, loop pass, `ops.reconcile_systems`), and `image_catalog`/inventory rows have **no** pre-existing per-object advisory lock (unlike Project/Allocation/System, which the existing `reconcile_once` serializes on). Two concurrent passes both "load existing → compute prune set → insert/delete" and race: concurrent inserts hit the `(provider,name,arch)` unique constraint → one transaction aborts (a spurious pass failure); prune+recreate can flap. So each pass holds a **session-scoped** inventory advisory lock for its **whole** duration — `pg_advisory_lock(LockScope.INVENTORY)` … `pg_advisory_unlock` in a `try/finally`. **It must be session-scoped, not `advisory_xact_lock`:** the pass spans *multiple* transactions (the batched upsert transaction + a separate per-image transaction per prune-with-reclaim, see Transaction boundaries below), and an xact lock auto-releases at the end of the first transaction — too early to serialize the prunes. Add a concurrent-pass test (two reconciles in flight → no unique-violation abort; second is a clean no-op).

`reconcile_images.py`:
- Load existing `image_catalog` rows.
- **The upsert must be change-detecting** (load-bearing for the idempotency invariant): compare the existing row's config-owned fields to the desired values and only write — and only append to `diff.updated` — when something actually differs. An unconditional `UPDATE … SET …` marks every row `updated` on every pass, fails `test_reconcile_is_idempotent`, and turns a steady state into perpetual phantom drift in the loop's reporting. The same "don't re-emit each pass" rule applies to **every derived warning** (`build`-base-not-registered, `s3`-missing-digest, `s3`-missing-object): warn-state is computed from the row's current state for the operator, not appended as a per-pass change — else a steady `defined` image spams the log every pass and any "warned = drift" check flaps.
- **Scope the identity match to config-eligible rows (load-bearing — `(provider,name,arch)` is NOT uniquely constrained):** `image_catalog` uniqueness is visibility/state-scoped (`0023` partial indexes `WHERE state='registered' AND visibility=…`), so a config public image and a project-private upload can share `(provider,name,arch)`. When loading "existing rows" to upsert/prune, **match only `managed_by='config'` rows** (equivalently `visibility='public' AND owner IS NULL`) — never resolve a `runtime`/private row as the upsert or prune target. Otherwise reconcile could overlay config onto, or prune, a user's private image. Test: a private upload sharing `(provider,name,arch)` with a config image is untouched.
- For each config `ImageEntry`: upsert keyed by `(provider, name, arch)` **among config rows**, writing only config-owned fields (`provider/name/arch/format/root_device/visibility/capabilities/managed_by='config'`), **never** `object_key`/`digest`/`state` when the existing row is already `registered` from a build (invariant 1).
  - `staged` → set `volume`, `state='registered'` (no S3).
  - `s3` → HEAD the object (existence). If digest supplied (config) → `state='registered'`, set `object_key`+`digest`; else leave `state='defined'` + append a `warned` entry (invariant 8). **Degrade on both "object absent" (404) AND "object store unconfigured/unreachable"** (a HEAD against an unwired store throws a client/connection error, not a clean 404 — matching the spec's `_seed_build_configs_step` no-S3 tolerance): catch both → row stays `defined` + warn, the pass still succeeds; it realizes on a later reconcile once S3 is up. Only a *configured-and-reachable-but-erroring* store is a hard failure. Test: no object store configured → row stays `defined`, pass succeeds (does not abort).
  - `build` → ensure a `defined` row exists; never downgrade a realized row (invariant 1). If the row's `base_image` is referenced but not yet `registered`, append a `warned`/degraded marker.
- Prune: for each existing `managed_by='config'` row whose `(provider,name,arch)` is absent from config, call `_prune_or_cordon`.
  - **Liveness guard must be kind-aware (load-bearing — not local-only):** `image_referenced_by_live_system` (`retention.py`) probes only the **`local-libvirt`** `provisioning_profile` rootfs section, so as-is it would report a live **remote** base image as unreferenced and prune could delete an in-use remote/staged image (and its bytes). The guard MUST cover every kind this design prunes — generalize the probe to match remote-libvirt references too, or guard remote/staged images by an active allocation/System on a resource whose `base_image` is this image (see Task 1.5). Until a kind is demonstrably covered, prune of that kind is **cordon-only** (never reclaim).
  - If referenced → **cordon** + `diff.cordoned` (no delete). If idle → delete, and **reclaim the S3 object inline** for `s3`/`build`-backed rows: `store.delete(object_key)` **then** `DELETE FROM image_catalog`, mirroring `expire_one_private_image` (object-before-row). A `staged` row (`object_key IS NULL`) just deletes the row.
  - **Why inline, not "GC owns it":** the existing reclamation (`expire_one_private_image`) is **catalog-driven** (it needs the row's `object_key`) **and private-only** (`visibility=private` + `expires_at`); there is no S3-enumeration orphan sweep and no public-image GC, so deleting a public config row without deleting its object would leak the bytes forever.
  - Runtime/discovery rows: skip (invariant 2/3).
- **Transaction boundaries (load-bearing — `store.delete` is not transactional):** batch the **upserts** in one transaction (all-or-nothing for the create/update phase). Run each **prune-with-reclaim in its own per-image transaction** (object-before-row, like `expire_one_private_image`) — do **not** issue inline `store.delete`s inside a single batch transaction, because a mid-batch rollback would revert row state while the S3 deletes already happened (irreversible). Per-image isolation means a crash leaves at most a dangling row the next pass re-prunes idempotently (`store.delete` of a missing key is a no-op).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/integration/test_reconcile_inventory.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/inventory/reconcile.py src/kdive/inventory/reconcile_images.py tests/integration/test_reconcile_inventory.py
git commit -m "feat(inventory): reconcile engine core + reconcile_images merge contract"
```

### Task 1.5: Extend the live-reference guard to cover every pruned kind

**No new schema.** The Task 1.4 prune cordon-guard builds on `image_referenced_by_live_system`
(`services/images/retention.py:60-83`). But that probe is **`local-libvirt`-only** (hardwired
`_LOCAL_LIBVIRT_SECTION`), and this design prunes **remote/staged** base images too — so reusing it
unchanged would report a live remote base image as unreferenced and let prune delete an in-use
image. This task makes the guard cover the kinds prune touches; it is a **correctness prerequisite**
for prune reclaiming any non-local image, not a confirmation step.

- [ ] **Step 1:** Read `image_referenced_by_live_system` / `_LOCAL_LIBVIRT_SECTION`; confirm the local-libvirt JSONB path.
- [ ] **Step 2:** Determine how a **live remote/staged System references its base image** — the remote-libvirt `provisioning_profile` section, or an active allocation/System on a resource whose `base_image` is this image. Generalize the probe (a kind-parameterized section, or a union with an allocation-based check) so a live System of **any** provider that uses the image returns referenced.
- [ ] **Step 3:** Write a **remote-dependent cordon test:** a live remote System on a `staged`/`s3` base image → reconcile **cordons** (does not delete/`store.delete`) that base image. Also make Task 1.4's `_attach_dependent_system` build the **real** profile shape so the local case exercises the production probe, not a stub.
- [ ] **Step 4:** Until a kind is demonstrably covered by the guard, prune of that kind stays **cordon-only** in Task 1.4 (never reclaim) — assert that fallback in a test.
- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(inventory): extend live-image reference guard to remote/staged base images before prune reclaims them"
```

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


async def test_loop_inventory_pass_skips_quietly_when_default_file_absent(pg_conn, monkeypatch, tmp_path):
    # systems.toml is gitignored; an absent DEFAULT file is the normal pre-config state and
    # must NOT mark the pass failed every loop iteration.
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "does-not-exist.toml"))
    report = await reconcile_once(pg_conn, _config_with_inventory_spec())
    assert "reconcile_inventory" not in report.failures     # absent default != failure
```

- [ ] **Step 2: Run to verify it fails.** `uv run pytest tests/integration/test_reconcile_inventory.py -k "fault_isolated or absent" -v` → FAIL.

- [ ] **Step 3: Implement.**
  - `_reconcile_inventory_pass(conn)`: read `KDIVE_SYSTEMS_TOML` (default `./systems.toml`); use `load_inventory_optional` — an **absent default file returns None → the pass is a quiet no-op** (nothing declared; do not record a failure). A **present-but-malformed** file raises `InventoryError`; catch + log + re-raise so the existing `_build_repairs` try/except records it as a failed-this-pass spec (sibling repairs keep running — the existing loop contract at `loop.py:350-356`).
  - **Drift repair vs the content-hash gate (load-bearing):** this pass is billed as the ADR-0021 *drift*-repair spec, so it must repair DB drift even when the **file is unchanged** (a config-owned row manually deleted/corrupted). A content-hash gate that skips the whole pass when the file is unchanged would silently negate that. Resolution: the hash gate may skip only the **parse/validate** step (cache the last-good `InventoryDoc` keyed by file hash); the **reconcile-against-DB** step then runs every pass against the cached doc. With change-detecting upserts (Task 1.4) a no-drift pass is already cheap (reads + diff, no writes), so a steady state costs a few selects, not a skip. Do **not** gate the reconcile step on file-hash.
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
- **Contract:** `managed_by` governs existence (`config` for declared `fault_inject`/`remote_libvirt` instances; `discovery` for host-probed `local-libvirt`); a **config overlay** applies declared attributes keyed by instance `name`, regardless of who created the row.
- **The overlay is split across a column and jsonb (load-bearing — `cost_class` is NOT jsonb):** `cost_class` → the top-level **`resources.cost_class` column** (`0001_init.sql`, NOT NULL, read by cost-coefficient resolution); `vcpus`/`memory_mb`/`concurrent_allocation_cap` → the **`capabilities` jsonb** (confirmed: `CONCURRENT_ALLOCATION_CAP_KEY` is a jsonb key). Writing `cost_class` into jsonb leaves the NOT NULL column unset (insert fails) or stale (pricing reads the column). The #385 test must assert sizing/cap land in jsonb **and** `cost_class` in the column.
- **Required columns on a config-created row:** `reconcile_resources` *creates* rows for `fault_inject`/`remote_libvirt` (only `local-libvirt` is discovery-created), so it must supply the NOT NULL `resources` columns that `systems.toml` doesn't carry: set `status = 'available'` and `pool = 'default'` on create (no per-instance `pool` field in the v2 schema — YAGNI; add one only when a multi-pool need is real). A discovery-created row keeps the values discovery already supplies.
- **Identity:** key by `(kind, name)` (the migration's unique index); `resource_id` UUID stays the PK/FK target.
- **Discovery bind:** a discovered row binds to its config instance by `host_uri` and inherits that instance's `name`; a discovered host with no config instance gets a deterministic `name` derived from `host_uri`.
- **One creator per kind (load-bearing — avoids Phase-2 double-create):** existence is owned by exactly one layer per kind. `local-libvirt`: **discovery creates**, config overlays the same row. `remote_libvirt`/`fault_inject`: **`reconcile_resources` is the sole creator** (managed_by='config'); the provider discovery for those kinds becomes **bind-only / non-creating** in Phase 2 (contributes hardware facts to the config row by `host_uri`, never inserts). Otherwise, in the Phase-2→3 window the legacy env-based remote discovery (`remote_libvirt/discovery.py`, not removed until Phase 3) and the config reconcile both create a row for the same host → a duplicate, or a `(kind,name)` partial-unique-index violation when the discovered row inherits the config name. Test: config + legacy discovery for one remote host → exactly one row.
- **New-row default (load-bearing):** the migration backfills *existing* `resources` to `managed_by='discovery'`, but the column **default is `'runtime'`**, so a host discovered **after** the migration would insert at the wrong layer. Resolve this explicitly: the discovery/registrar insert path must write `managed_by='discovery'` on insert (or reconcile owns all resource creation and discovery never inserts directly — pick one and state it in the discovery code). Do **not** rely on the column default for discovered rows. Add a test: a freshly discovered host (post-migration insert) lands `managed_by='discovery'`, not `'runtime'`.
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
- **Identity + collision:** key the upsert by the build-host **`name`** — already `text UNIQUE NOT NULL` (`0027_build_hosts.sql:8`), no migration change needed. A config-declared host whose `name` matches an existing `managed_by='runtime'` row → **adopt** (flip to `config`), mirroring the resource adopt-on-collision (Task 4.4); a runtime `build_hosts.register` of a name that already exists as a `config` row is **rejected**. (The seeded baseline `build_hosts` row from `0027` is `managed_by='runtime'`; declaring it in config adopts it.)
- **Prune is DB-guarded too:** `build_host_leases` FKs `build_hosts(id)` `ON DELETE RESTRICT` (`0027`), so a host with an in-flight build lease **cannot** be deleted — prune of a busy host must **cordon** (the RESTRICT would otherwise abort the pass), naturally matching the refuse-if-live contract.
- **Test:** a `[[build_host]]` reconciles to a `build_hosts` row; an existing runtime host declared in config is adopted, not duplicated; a config host with an in-flight lease is cordoned, not deleted.

### Task 3.3: Delete the singleton remote env vars
- **Delete:** `KDIVE_REMOTE_LIBVIRT_{URI,CLIENT_CERT_REF,CLIENT_KEY_REF,GDB_ADDR,BASE_IMAGE}` reads in `src/kdive/providers/remote_libvirt/{config.py,settings.py,discovery.py}` and their consumers; the remote provider now resolves its connection from the reconciled `resources` row (per instance). Delete `scripts/coverage_campaign/d1.env.template`.
- **Extend the guard test** (`tests/guards/test_no_inventory_in_code.py`): assert no `KDIVE_REMOTE_LIBVIRT_*` singleton env reads remain in `src/kdive/providers/remote_libvirt/`.
- **Note:** several remote lifecycle modules (`install.py`, `provisioning.py`, `build_vm.py`) read `KDIVE_REMOTE_LIBVIRT_*` "per op" (ADR-0076). Each must be rewired to take the instance's config from the resolved resource row — this is the largest mechanical change in Phase 3; budget it as its own sub-issue.

### Task 3.4: Update the coverage-campaign consumer to schema v2
- **Files:** `scripts/coverage_campaign/systems.py` — today it reads the pre-v2 `systems.toml`. Now that the file is `schema_version = 2` (Phase 1) and `d1.env.template` is removed (Task 3.3), the campaign `render-env`/`setup-commands` consumer must parse v2 — ideally by **reusing `kdive.inventory.loader.load_inventory`** rather than a second parser, so the file has exactly one schema.
- **Test:** the campaign `render-env`/`setup-commands` subcommands round-trip a v2 `systems.toml`.
- **If deferred:** if updating the campaign tooling is out of scope for this milestone, say so explicitly here and note the campaign reader is temporarily broken until a follow-up — do not leave it silently broken by the v2 cutover.

**Phase-3 done check:** multiple instances per provider expressible + allocatable; guard test rejects `KDIVE_REMOTE_LIBVIRT_*`; `just test` green.

---

# Phase 4 — runtime inventory mutation (agent-native)

Outcome: an agent can add/remove a system live, scoped to its project, with leaked additions auto-reaped. Built on the Phase-1 migration columns (`affinity`/`owner_project`/`affinity_allowlist`/`lease_expires_at`).

### Task 4.1: `resources.register` / `deregister` / `renew` tools
- **Files:** create `src/kdive/mcp/tools/ops/resources/{register.py,deregister.py,renew.py}`, mirroring `src/kdive/mcp/tools/ops/build_hosts/register.py`.
- **Auth:** all `platform_admin`, mutating (adding shared capacity). `deregister` of a resource with **live allocations** is **destructive-tier** (platform_admin + typed confirmation / `--force`), like `ops.force_teardown`.
- **register:** same fields as a `[[remote_libvirt]]`/`[[local_libvirt]]`/`[[fault_inject]]` block; **preflight is per-kind** — `remote_libvirt`: reachability probe + cert/secret refs resolve + `base_image` is `registered`; `local_libvirt`: host reachability + (no base_image); `fault_inject`: secret ref resolves only (synthetic — no reachability, no base_image). Do not fail a fault-inject register on a missing `base_image`. Then write a `managed_by='runtime'` row; default `owner_project` to the **registering project**. Reject a `name` that already exists as a `config` row.
- **deregister:** operate **only** on `runtime` rows; reject a `config`-owned instance (config is removed by editing the file).
- **renew:** keyed to `resource_id` (not the registering session — survives agent handoff); extends `lease_expires_at`.
- **Lease TTL:** `register` sets `lease_expires_at = now() + TTL` and `renew` extends it; the TTL has a named default in the `KDIVE_*` registry (e.g. `KDIVE_RESOURCE_LEASE_TTL`, mirroring the project-private image `expires_at` TTL the spec cites), not a magic constant. Name it in the config registry so the reap timing (the leak-resistance guarantee) is explicit and tunable.
- **Test:** register→allocate→renew→deregister round-trip; config-row deregister rejected; live-allocation deregister requires `--force`.

### Task 4.2: Per-project affinity — selection filter + admission backstop
- **Files:** `src/kdive/services/allocation/placement.py` (**selection**) **and** `src/kdive/services/allocation/admission.py` (`admit()`/`_admit_under_project_lock`, l.160/296, backstop).
- **Affinity predicate:** a project may place only on a **global** resource (`owner_project IS NULL`) or one it owns / is on the `affinity_allowlist` of.
- **Enforce at BOTH layers (load-bearing):** the **selection** path (`placement.py`) resolves a concrete resource for an "any-available by `kind`" request *before* `admit()` runs. If affinity is checked only at admission, an any-available request can select a **scoped** instance and then be hard-denied — instead of falling through to a global instance the project may legally use. So the affinity predicate must **filter the any-available candidate set** in `placement.py` (exclude disallowed resources from selection), with the `admit()` check as the backstop for explicit `resource_id` requests.
- **Default-global no-op:** every pre-existing discovered resource and config-declared instance has `owner_project NULL` (Phase-1 backfill), so both checks are a strict no-op for current behavior — **no allocation that works today regresses.**
- **Test:** a scoped runtime resource rejects a foreign project (explicit `resource_id` → denied at admit); an **any-available** request that would otherwise pick a scoped instance **skips it and lands on a global one** (selection filter); a global resource admits any project; an allowlisted project is admitted; the regression test that all current (global) allocations still pass.

### Task 4.3: Lease + reachability reaping (reconciler reap spec)
- **Files:** new reconciler `_RepairSpec("reap_runtime_resources", ...)` in `loop.py`; reuse the #359 reachability probe.
- **Contract:** reap a `managed_by='runtime'` resource on **lease expiry OR sustained unreachability past TTL** — but **cordon-only / refuse-if-live, never auto-drain** (identical to the config-prune contract; preserves a `crashed` System under live crash-debug per ADR-0109). Config/discovery rows carry no lease and are never lease-reaped.
- **Test (invariant 6 + reap):** a runtime resource past its lease with no live allocation is reaped; one with a live allocation is cordoned + surfaced, not destroyed.

### Task 4.4: Adopt-on-collision in the reconcile engine
- **Files:** `src/kdive/inventory/reconcile_resources.py`.
- **Contract:** a config identity (`name`) matching a `runtime` row → adopt: flip `managed_by → config`, **clear `lease_expires_at`**, **take the config-declared affinity** (default global). Registration + reconcile **serialize on the identity** (advisory lock on `name`) so prune cannot race re-`register`.
- **Test (invariant 6):** adoption clears the lease and applies config affinity (widening a previously project-scoped runtime resource to global unless the file declares a scope).

### Task 4.5: `ops.reconcile_systems` MCP trigger (gated + audited)
- The existing `ops.reconcile_now` is gated `platform_operator` and **audits to `platform_audit_log`** (`mcp/tools/ops/reconcile.py`). Because the inventory pass can **prune** (and reclaim S3 bytes), an inventory-triggering MCP path must be `platform_admin`. **Default: a dedicated `ops.reconcile_systems` tool** (platform_admin) — keeps `reconcile_now`'s existing contract untouched.
- **Audit (load-bearing — it's destructive-tier):** `ops.reconcile_systems` audits to `platform_audit_log` like `reconcile_now`, recording the actor and the resulting `ReconcileDiff` (especially prunes and object reclamations), so a config-driven deletion is attributable.

**Phase-4 done check:** agent register→use→handoff-renew→deregister works; leaked runtime resource auto-reaped (cordon-if-live); affinity no-op proven for existing allocations; `just test` green.

---

## Epic / sub-issue decomposition (drives the coding)

One **epic** + sub-issues, one milestone. Sub-issues are sized for parallel `/work-issue` worktree agents. **Pre-assign disjoint ADR/migration numbers in the dispatch prompt** (lesson: parallel agents otherwise all grab "next free" — see `preassign-adr-migration-numbers` memory). Only the Phase-1 migration `0030` exists; later phases author **no new migrations** (they populate Phase-1 columns) — make that explicit in each sub-issue to prevent migration-number collisions.

Epic: **#387** (milestone M2.6 — Systems inventory config).

| # | Issue | Sub-issue | Phase | Depends on | Files (primary) |
|---|---|---|---|---|---|
| A | #388 | Migration `0030` (all schema; no system→image col — reuse existing probe) | 1 | — | `db/schema/0030_*.sql`, `test_migrate.py` |
| B | #389 | Inventory model + loader | 1 | — (parallel with A) | `inventory/{model,loader,errors}.py` |
| C | #390 | Engine core + `reconcile_images` | 1 | #388, #389 | `inventory/{reconcile,reconcile_images}.py` |
| D | #391 | CLI + `reconcile_inventory` loop pass | 1 | #390 | `cli/reconcile_systems.py`, `reconciler/loop.py` |
| E | #392 | Delete in-code image defs + guard test | 1 | #390, #391 | `images/seed_data/` (del), `default_fixtures.py`, `rootfs_build.py`, `tests/guards/` |
| F | #393 | `reconcile_resources` overlay + #385 fix | 2 | #390, #392 | `inventory/reconcile_resources.py`, `fault_inject/discovery.py` |
| G | #394 | Multi-instance + `reconcile_build_hosts` | 3 | #393 | `inventory/reconcile_build_hosts.py` |
| H | #395 | Delete `KDIVE_REMOTE_LIBVIRT_*` singletons + rewire remote lifecycle | 3 | #394 | `providers/remote_libvirt/*`, guard test |
| I | #396 | `resources.register/deregister/renew` tools | 4 | #393 | `mcp/tools/ops/resources/*` |
| J | #397 | Affinity selection filter + admission backstop | 4 | #388 | `services/allocation/placement.py`, `services/allocation/admission.py` |
| K | #398 | Lease + reachability reaping spec | 4 | #388, #396 | `reconciler/loop.py` |
| L | #399 | Adopt-on-collision + `ops.reconcile_systems` gating | 4 | #390, #396, #397 | `inventory/reconcile_resources.py`, `mcp/tools/ops/` |

**Wave plan:** Wave 1 = {#388, #389} parallel. Wave 2 = {#390}. Wave 3 = {#391, #392}. Phase-1 ships. Wave 4 = {#393}. Wave 5 = {#394}, then {#395}. Phase-2/3 ship. Wave 6 = {#396, #397} parallel, then {#398, #399}. Phase-4 ships.

**Recurring rebase zones** (serialize merges here, per prior orchestration lessons): `reconciler/loop.py` (D, K), `mcp/tools/ops/` registry + generated tool docs (I, L), `services/allocation/` (J touches `placement.py`+`admission.py`; F touches `reconcile_resources.py`+`fault_inject/discovery.py`, **not** `admission.py` — the #385 fix is the capabilities overlay), `tests/integration/test_migrate.py` (A only — no later migrations).

---

## Self-review

- **Spec coverage:** three ownership layers → Tasks 1.1 (managed_by) + 2.1 (overlay) + 4.2 (affinity). Operating model (declarative/imperative disjoint) → 1.1 backfill + 4.1 runtime rows. Adopt-on-collision → 4.4 (resources) + 3.2 (build hosts). Three reload triggers → 1.6 (CLI + loop) + 4.5 (MCP, gated+audited). Engine parser/validator → 1.2/1.3. Per-entity reconcilers → 1.4/2.1/3.2. Existence-vs-overlay split → 2.1. Image realization (s3/build/staged + CHECK relax) → 1.1 + 1.4. Non-destructive prune (kind-aware live-reference guard, reuse `image_referenced_by_live_system`; inline per-image S3 reclaim; rollout grace valve) → 1.4 + 1.5. Runtime mutation (register/deregister/renew + auth) → 4.1. Affinity (selection + admission) → 4.2. Lease/reaping → 4.3. Schema v2 → 1.2. All four phases → Phases 1–4. Deletes → 1.7 + 2.2 + 3.3. Campaign v2 consumer → 3.4. Error handling (fault-isolation, missing-default tolerance, s3 store-unconfigured degrade) → 1.6 + 1.4. All 8 test invariants → 1.4 (1,2,3,5,8 + 7 via `test_relaxed_check_rejects_both_or_neither`), 2.1 (4), 4.3/4.4 (6). Guard test → 1.7 + 3.3. Every spec section maps to a task.
- **Placeholder scan:** Phase 1 carries complete code (migration SQL, model, loader, test bodies). Phases 2–4 are sub-issue specs with concrete file paths + test invariants, each getting its own bite-sized plan at execution (per the spec's "four phases, each its own implementation plan"). No "TBD"/"add validation"/"handle edge cases" left unspecified.
- **Type consistency:** `InventoryDoc`/`ImageEntry`/`ImageSource`/`*Instance` names match across model, loader, and reconcile tasks. `ReconcileDiff` fields (`created/updated/pruned/cordoned/warned`) are used consistently in 1.4's tests and 1.6/2.1. `managed_by` values (`config/discovery/runtime`) match the migration CHECK. `(provider,name,arch)` for images vs `(kind,name)` for resources vs `name` (already-UNIQUE) for build hosts is consistent throughout. No `systems.image_id` column — the prune guard reuses the existing `image_referenced_by_live_system` probe (extended per Task 1.5 to cover remote/staged).
