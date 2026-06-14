# Kdump config-fragment provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision a canonical kdump kernel-config fragment as a seeded, object-store-backed catalog input that the local build, the remote build, and an inline MCP tool all resolve identically, so a from-source kernel is kdump-capable.

**Architecture:** One packaged fragment is published once to the object store under a fixed reserved key and recorded in a new `build_config_catalog` table. Both build providers stop staging a complete `.config` and instead `make defconfig` → `merge_config.sh -m` the fragment → single `make olddefconfig` → fragment-survival check. A `CatalogComponentRef` config ref (or an implicit `kdump` default) resolves through the catalog; an inline `buildconfig.get` tool serves the same bytes.

**Tech Stack:** Python 3.13, `uv`/`ruff`/`ty`/`pytest`, psycopg (async), boto3 (`ObjectStore`), FastMCP, forward-only SQL migrations (ADR-0015).

**Delivery:** One PR on branch `plan/kdump-config-provisioning` (already holds the spec+ADR commits), four scoped commits (one per task), merged `--rebase` — never squashed (preserves `git bisect`).

**Spec:** `docs/superpowers/specs/2026-06-11-kdump-config-provisioning-design.md` · **ADR:** `docs/adr/0096-kdump-config-fragment-build-input.md`

**Guardrails (run before every commit):**
```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest -q <the task's test files>
```
Zero warnings. The MCP tool task additionally runs `just docs-check`.

---

## File structure

**Create:**
- `src/kdive/build_configs/__init__.py` — package for the build-config catalog (mirrors `images/`).
- `src/kdive/build_configs/data/kdump.config` — the packaged fragment (the source of truth; packaged under `src/` so it ships in the wheel/container, the lesson `images/seed_data/` already learned — the spec's illustrative `provisioning/configs/` path is repo-root and would not be on a deployed image).
- `src/kdive/build_configs/catalog.py` — the catalog **repository**: `name` → sha256-verified bytes, plus the row model.
- `src/kdive/build_configs/seed.py` — `seed_build_configs(conn, store)`: publish the packaged fragment to a fixed reserved object-store key + upsert the row, idempotent.
- `src/kdive/db/schema/0025_build_config_catalog.sql` — the table.
- `src/kdive/mcp/tools/catalog/build_configs.py` — the `buildconfig.get` read tool.
- Tests: `tests/build_configs/test_catalog.py`, `tests/build_configs/test_seed.py`, `tests/mcp/catalog/test_build_configs_tool.py`, plus additions to the providers' build tests.

**Modify:**
- `src/kdive/admin/bootstrap.py` — call `seed_build_configs` alongside `_seed_baseline_rootfs`.
- `src/kdive/providers/remote_libvirt/build.py` and `src/kdive/providers/local_libvirt/build.py` — `_resolve_config_ref` catalog branch; replace `_stage_config` with `_merge_config`; wire an injected config-catalog fetch.
- `src/kdive/providers/composition.py` — `CONFIG_COMPONENT: {"local", "catalog"}` (both providers).
- `src/kdive/profiles/build.py` — `ServerBuildProfile.config` → `ComponentRef | None`; default-resolution at the build boundary.
- `src/kdive/mcp/tools/catalog/__init__.py` (or the tool registry) — register `buildconfig.get`.
- `tests/integration/_seed.py` — replace the dead `/configs/kdump.config` ref.
- `docs/runbooks/` — the four-method live-run runbook.

---

## Task 1: Fragment + table + seed + catalog repository

This task is purely additive: a new table, a packaged fragment, a repository, and a seed. Nothing consumes the repository yet (Task 2 wires it into the providers; Task 3 into the tool) — it is exercised by its own tests here.

**Files:**
- Create: `src/kdive/build_configs/__init__.py`, `src/kdive/build_configs/data/kdump.config`, `src/kdive/build_configs/catalog.py`, `src/kdive/build_configs/seed.py`, `src/kdive/db/schema/0025_build_config_catalog.sql`
- Modify: `src/kdive/admin/bootstrap.py`
- Test: `tests/build_configs/test_catalog.py`, `tests/build_configs/test_seed.py`

- [ ] **Step 1: Write the migration**

Create `src/kdive/db/schema/0025_build_config_catalog.sql`:

```sql
-- Build-config catalog (ADR-0096): one row per seeded kernel-config fragment.
-- object_key points at a fixed reserved object-store key (system/build-configs/<name>/...),
-- NOT a project-scoped artifacts row. sha256 binds the row to the published bytes.
CREATE TABLE build_config_catalog (
    name        text PRIMARY KEY,
    object_key  text NOT NULL,
    sha256      text NOT NULL,
    description text NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 2: Write the packaged fragment**

Create `src/kdive/build_configs/data/kdump.config` (the final symbol set is pinned in Step 8 against a real `make olddefconfig`; this is the starting set):

```
CONFIG_KEXEC=y
CONFIG_KEXEC_CORE=y
CONFIG_CRASH_DUMP=y
CONFIG_PROC_VMCORE=y
CONFIG_RELOCATABLE=y
CONFIG_RANDOMIZE_BASE=y
CONFIG_DEBUG_INFO=y
CONFIG_DEBUG_INFO_DWARF5=y
CONFIG_DEBUG_KERNEL=y
CONFIG_MAGIC_SYSRQ=y
```

Create `src/kdive/build_configs/__init__.py`:

```python
"""Build-config catalog: seeded kernel-config fragments as build inputs (ADR-0096)."""
```

- [ ] **Step 3: Write the failing repository test**

Create `tests/build_configs/test_catalog.py`:

```python
from __future__ import annotations

import base64
import hashlib

import pytest

from kdive.build_configs.catalog import BuildConfigEntry, parse_build_config_row
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_parse_build_config_row_round_trips_fields() -> None:
    entry = parse_build_config_row(
        {"name": "kdump", "object_key": "system/build-configs/kdump/kdump.config",
         "sha256": "abc", "description": "kdump options"}
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
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `uv run pytest -q tests/build_configs/test_catalog.py`
Expected: FAIL — `ModuleNotFoundError: kdive.build_configs.catalog`.

- [ ] **Step 5: Write the catalog repository**

Create `src/kdive/build_configs/catalog.py`:

```python
"""Build-config catalog repository (ADR-0096): name -> sha256-verified fragment bytes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.errors import CategorizedError, ErrorCategory


@dataclass(frozen=True)
class BuildConfigEntry:
    """One build_config_catalog row."""

    name: str
    object_key: str
    sha256: str
    description: str

    def verify_bytes(self, data: bytes) -> None:
        """Raise INFRASTRUCTURE_FAILURE if ``data`` does not hash to this row's ``sha256``."""
        actual = hashlib.sha256(data).hexdigest()
        if actual != self.sha256:
            raise CategorizedError(
                "build-config object bytes do not match the catalog sha256",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"name": self.name},
            )


def parse_build_config_row(row: dict[str, Any]) -> BuildConfigEntry:
    """Map a DB row mapping to a :class:`BuildConfigEntry`."""
    return BuildConfigEntry(
        name=row["name"],
        object_key=row["object_key"],
        sha256=row["sha256"],
        description=row["description"],
    )


_SELECT = (
    "SELECT name, object_key, sha256, description "
    "FROM build_config_catalog WHERE name = %(name)s"
)


async def get_build_config(conn: AsyncConnection, name: str) -> BuildConfigEntry | None:
    """Return the catalog entry for ``name``, or ``None`` if absent (async, for the MCP tool)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SELECT, {"name": name})
        row = await cur.fetchone()
    return parse_build_config_row(row) if row is not None else None


def get_build_config_sync(conn: Connection, name: str) -> BuildConfigEntry | None:
    """Return the catalog entry for ``name``, or ``None`` (sync, for the build path).

    The provider build runs off the event loop via ``asyncio.to_thread`` and cannot await, so
    its catalog fetch uses a synchronous connection. Same query as the async variant.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT, {"name": name})
        row = cur.fetchone()
    return parse_build_config_row(row) if row is not None else None
```

Add `from psycopg import AsyncConnection, Connection` to the imports (replacing the `AsyncConnection`-only import). Add a sync test to `tests/build_configs/test_catalog.py` mirroring the async one (insert a row via the DB harness, assert `get_build_config_sync` returns the entry, and `None` for an absent name).

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest -q tests/build_configs/test_catalog.py`
Expected: PASS (3 tests).

- [ ] **Step 7: Write the failing seed test**

Create `tests/build_configs/test_seed.py`. The seed writes bytes through an `ObjectStore` and upserts a row; use a fake store and an in-memory connection double matching the project's existing seed-test style (see `tests/images/` for the connection fixture). Key behaviors:

```python
from __future__ import annotations

import hashlib

import pytest

from kdive.build_configs.seed import KDUMP_FRAGMENT_PATH, seed_build_configs


def test_kdump_fragment_is_packaged_and_nonempty() -> None:
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    assert data.strip()
    assert b"CONFIG_CRASH_DUMP=y" in data


@pytest.mark.asyncio
async def test_seed_publishes_fragment_and_upserts_row(fake_conn, fake_store) -> None:
    published = await seed_build_configs(fake_conn, fake_store)
    assert published == 1
    expected_sha = hashlib.sha256(KDUMP_FRAGMENT_PATH.read_bytes()).hexdigest()
    row = fake_conn.upserted_rows["kdump"]
    assert row["sha256"] == expected_sha
    assert row["object_key"] == "system/build-configs/kdump/kdump.config"
    assert fake_store.put_keys == ["system/build-configs/kdump/kdump.config"]


@pytest.mark.asyncio
async def test_seed_is_idempotent_when_sha_unchanged(fake_conn, fake_store) -> None:
    fake_conn.existing_sha["kdump"] = hashlib.sha256(KDUMP_FRAGMENT_PATH.read_bytes()).hexdigest()
    published = await seed_build_configs(fake_conn, fake_store)
    assert published == 0
    assert fake_store.put_keys == []  # no re-put when sha matches


@pytest.mark.asyncio
async def test_seed_overwrites_in_place_on_changed_bytes(fake_conn, fake_store) -> None:
    fake_conn.existing_sha["kdump"] = "stale-sha"
    await seed_build_configs(fake_conn, fake_store)
    # fixed reserved key -> same key overwritten, no orphan
    assert fake_store.put_keys == ["system/build-configs/kdump/kdump.config"]
```

Define `fake_conn`/`fake_store` fixtures in `tests/build_configs/conftest.py` modeling: `fake_conn.existing_sha` (name→sha lookup the seed reads), `fake_conn.upserted_rows` (name→row the seed writes), and `fake_store.put_keys` / `fake_store.put_artifact(request)` capturing `request.key()`.

- [ ] **Step 8: Run the test to verify it fails**

Run: `uv run pytest -q tests/build_configs/test_seed.py`
Expected: FAIL — `ModuleNotFoundError: kdive.build_configs.seed`.

- [ ] **Step 9: Write the seed**

Create `src/kdive/build_configs/seed.py`:

```python
"""App-level build-config seed (ADR-0096).

The SQL migration creates the table; this step publishes the packaged kdump fragment to a
fixed reserved object-store key and upserts the catalog row, idempotently. The bytes go to the
object store via ``put_artifact`` (object-store write only — NOT ``register_artifact_row``, so
no project-scoped artifacts row and none of its TTL/owner lifecycle, per ADR-0096). The reserved
key is deterministic in (tenant, owner_kind, owner_id, name), so an edited fragment overwrites
in place — no orphaned object. ``Sensitivity.REDACTED`` marks the fragment serve-eligible (the
``buildconfig.get`` tool serves it); it carries no secret.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactWriteRequest
from kdive.store.objectstore import ObjectStore

KDUMP_FRAGMENT_PATH = Path(__file__).parent / "data" / "kdump.config"
_KDUMP_NAME = "kdump"
_KDUMP_DESCRIPTION = "kdump/debuginfo kernel-config fragment"
_TENANT = "system"
_OWNER_KIND = "build-configs"
_RETENTION_CLASS = "build-config"


async def _stored_sha(conn: AsyncConnection, name: str) -> str | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT sha256 FROM build_config_catalog WHERE name = %(name)s", {"name": name}
        )
        row = await cur.fetchone()
    return row["sha256"] if row is not None else None


async def _upsert(conn: AsyncConnection, name: str, object_key: str, sha256: str, desc: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO build_config_catalog (name, object_key, sha256, description) "
            "VALUES (%(name)s, %(object_key)s, %(sha256)s, %(description)s) "
            "ON CONFLICT (name) DO UPDATE SET "
            "object_key = EXCLUDED.object_key, sha256 = EXCLUDED.sha256, "
            "description = EXCLUDED.description, updated_at = now()",
            {"name": name, "object_key": object_key, "sha256": sha256, "description": desc},
        )


async def seed_build_configs(conn: AsyncConnection, store: ObjectStore) -> int:
    """Publish the packaged kdump fragment + upsert its row. Returns the count published (0 or 1).

    Idempotent: when the stored sha256 already matches the packaged bytes, nothing is written.
    """
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    if await _stored_sha(conn, _KDUMP_NAME) == sha256:
        return 0
    stored = store.put_artifact(
        ArtifactWriteRequest(
            tenant=_TENANT,
            owner_kind=_OWNER_KIND,
            owner_id=_KDUMP_NAME,
            name="kdump.config",
            data=data,
            sensitivity=Sensitivity.REDACTED,
            retention_class=_RETENTION_CLASS,
        )
    )
    await _upsert(conn, _KDUMP_NAME, stored.key, sha256, _KDUMP_DESCRIPTION)
    return 1
```

> Note: confirm `StoredArtifact`'s key attribute name (`stored.key`) against `kdive/store/artifact_types.py`; `objectstore.put_artifact` returns it as the first positional in the `remote_libvirt/build.py:_put` usage.

- [ ] **Step 10: Run the seed test to verify it passes**

Run: `uv run pytest -q tests/build_configs/test_seed.py`
Expected: PASS.

- [ ] **Step 11: Wire the seed into bootstrap**

In `src/kdive/admin/bootstrap.py`, add a sibling to `_seed_baseline_rootfs` using the **exact** idiom that function uses (verified at `bootstrap.py:55-69`: `asyncio.run` + `psycopg.AsyncConnection.connect(database_url, autocommit=True)`):

```python
def _seed_build_configs_step(database_url: str) -> int:
    """Publish the packaged build-config fragments after migrating (ADR-0096).

    Runs in the deploy ``migrate -> seed`` step. Idempotent (sha256-gated).
    """
    import asyncio

    from kdive.build_configs.seed import seed_build_configs
    from kdive.store.objectstore import object_store_from_env

    store = object_store_from_env()

    async def _run() -> int:
        async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
            return await seed_build_configs(conn, store)

    return asyncio.run(_run())
```

Then call it from `migrate()` right after the rootfs seed (`bootstrap.py:50-51`), matching the print line:

```python
    seeded_configs = _seed_build_configs_step(url)
    print(f"seeded {seeded_configs} build-config fragment(s)")
```

- [ ] **Step 12: DB-backed migration + seed test (validates the SQL, not just fakes)**

The fake-conn tests in Steps 3/7 do not apply `0025_build_config_catalog.sql`, so a malformed migration or a wrong `ON CONFLICT` target ships uncaught. Add a DB-backed test under `tests/db/` (or `tests/build_configs/test_seed_db.py`) using the **existing** Postgres test harness — mirror `tests/db/test_image_catalog_migration.py` (migration-applies + columns exist) and `tests/images/test_seed.py` (seed against a real connection). It must: apply migrations via the project harness, run `seed_build_configs(conn, store)` against a real connection with a fake/local object store, assert the `build_config_catalog` row exists with the right `object_key`/`sha256`, and re-run to assert idempotency (`returns 0`, no second put). This is the test that actually exercises the upsert's `ON CONFLICT (name)` clause.

> Mirror the real fixtures: the unit `fake_conn`/`fake_store` shapes in Steps 7/9 are illustrative — follow `tests/images/test_seed.py` and `tests/db/test_harness.py` for the project's actual connection/object-store test doubles rather than inventing new fixture shapes.

- [ ] **Step 13: Run guardrails**

```bash
uv run ruff check . && uv run ruff format --check . && uv run ty check
uv run pytest -q tests/build_configs tests/db -k "build_config or migrate"
```
Expected: all green (the DB-backed test requires the project's test Postgres — same as the existing `tests/db/` suite).

- [ ] **Step 14: Commit**

```bash
git add src/kdive/build_configs src/kdive/db/schema/0025_build_config_catalog.sql \
        src/kdive/admin/bootstrap.py tests/build_configs
git commit -m "feat(build-config): seed a kdump config-fragment catalog

Add build_config_catalog table, the packaged kdump fragment, the catalog
repository (name -> sha256-verified bytes), and a bootstrap seed that
publishes the fragment to a fixed reserved object-store key. Nothing
consumes it yet (wired into the providers in the next commit).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Build-flow change + resolver + schema (both providers)

The load-bearing task. Replace `_stage_config` (copy a full `.config`) with `_merge_config` (`make defconfig` → `merge_config.sh -m` → single `olddefconfig` → fragment-survival check), add a `catalog` branch to `_resolve_config_ref`, admit `catalog` in `composition.py`, make `ServerBuildProfile.config` optional with a `kdump` default, and fix the integration seed in lockstep.

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/build.py`, `src/kdive/providers/local_libvirt/build.py`, `src/kdive/providers/composition.py`, `src/kdive/profiles/build.py`, `tests/integration/_seed.py`
- Test: `tests/providers/remote_libvirt/test_build.py`, `tests/providers/local_libvirt/test_build.py`, `tests/profiles/test_build.py`

- [ ] **Step 1: Write the failing fragment-survival test (remote)**

In `tests/providers/remote_libvirt/test_build.py`, add a pure-function test for the survival check (drive it directly with injected text, the project's existing build-test style):

```python
from kdive.providers.remote_libvirt.build import _dropped_fragment_symbols


def test_dropped_fragment_symbols_reports_a_dropped_option() -> None:
    fragment = "CONFIG_CRASH_DUMP=y\nCONFIG_PROC_VMCORE=y\n# a comment\n"
    final = "CONFIG_CRASH_DUMP=y\n# CONFIG_PROC_VMCORE is not set\n"
    assert _dropped_fragment_symbols(fragment, final) == ["CONFIG_PROC_VMCORE"]


def test_dropped_fragment_symbols_empty_when_all_survive() -> None:
    fragment = "CONFIG_CRASH_DUMP=y\n"
    final = "CONFIG_CRASH_DUMP=y\nCONFIG_OTHER=y\n"
    assert _dropped_fragment_symbols(fragment, final) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest -q tests/providers/remote_libvirt/test_build.py -k dropped_fragment`
Expected: FAIL — `_dropped_fragment_symbols` not defined.

- [ ] **Step 3: Implement `_dropped_fragment_symbols`**

In `src/kdive/providers/remote_libvirt/build.py`:

```python
def _fragment_symbols(fragment_text: str) -> list[str]:
    """The ``CONFIG_X`` names a fragment sets to ``=y``/``=m`` (ignoring comments/blank lines)."""
    symbols = []
    for line in fragment_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if value in ("y", "m"):
            symbols.append(name)
    return symbols


def _dropped_fragment_symbols(fragment_text: str, final_config_text: str) -> list[str]:
    """Fragment symbols absent from the final ``.config`` (dropped by olddefconfig)."""
    enabled = {
        line.split("=", 1)[0]
        for line in final_config_text.splitlines()
        if line and not line.startswith("#") and line.rstrip().endswith(("=y", "=m"))
    }
    return [sym for sym in _fragment_symbols(fragment_text) if sym not in enabled]
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest -q tests/providers/remote_libvirt/test_build.py -k dropped_fragment`
Expected: PASS.

- [ ] **Step 5: Replace `_stage_config` with `_merge_config` (remote)**

In `src/kdive/providers/remote_libvirt/build.py`, replace `_stage_config` (the `shutil.copyfile(source, workspace/".config")` function near line 422) with a `_merge_config` that writes the resolved fragment bytes to a temp file and runs the merge sequence. Keep it `# pragma: no cover - live_vm` like its siblings (the `_real_*` make wrappers):

```python
def _merge_config(  # pragma: no cover - live_vm
    fragment_bytes: bytes, workspace: Path, run_id: UUID
) -> None:
    """Base defconfig + merge the kdump fragment + single olddefconfig.

    ``merge_config.sh -m`` merges only (no internal olddefconfig); a single ``make olddefconfig``
    then resolves once against this tree, so the final ``.config`` is authoritative. The caller
    runs the fragment-survival check against that final ``.config``. ``run_id`` flows into
    ``_build_failure`` (which requires it for the ledger details) — there is no sentinel id.
    """
    if _run_make_target(workspace, ["defconfig"], "make defconfig") != 0:
        raise _build_failure("make defconfig exited non-zero", run_id)
    fragment_path = workspace / "kdump.config.fragment"
    fragment_path.write_bytes(fragment_bytes)
    merge = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["scripts/kconfig/merge_config.sh", "-m", ".config", str(fragment_path)],
        cwd=workspace, capture_output=True, text=True, check=False,
        timeout=_MAKE_TIMEOUT_S,
    )
    if merge.returncode != 0:
        raise _build_failure("merge_config.sh -m exited non-zero", run_id)
```

> **Data flow (pin this exactly — the resolve happens ONCE).** `build(run_id, profile, ...)` resolves the config ref to `fragment_bytes` ONCE, before checkout, via `_resolve_config_bytes(config_ref, allowed_component_roots=..., catalog_fetch=self._catalog_fetch)` (Step 7), where `config_ref = profile.config or CatalogComponentRef(kind="catalog", name="kdump")`. It then passes `fragment_bytes` into `_checkout(...)` → `_merge_config(fragment_bytes, workspace, run_id)` (the rsync/patch parts of `_checkout` are unchanged; only the old `_stage_config` call becomes `_merge_config`). The same `fragment_bytes` is decoded once to `fragment_text = fragment_bytes.decode()` and reused for the survival check (Step 6). So the ordering inside `build()` is: resolve `fragment_bytes` → `_checkout(..., fragment_bytes)` → `_run_olddefconfig` → `final = _read_config` → survival check(`fragment_text`, `final`) → `_missing_config_groups(final)` → `make`. Do **not** resolve the ref a second time in `_checkout`.

- [ ] **Step 6: Add the survival check into `build()` (remote)**

In `build()`, immediately after `missing = _missing_config_groups(...)` is computed (near line 178), insert the survival check **before** the preflight, reading the same `.config` text:

```python
        final_config = self._read_config(workspace)
        dropped = _dropped_fragment_symbols(fragment_text, final_config)
        if dropped:
            raise CategorizedError(
                "kdump fragment symbols were dropped by olddefconfig (unmet base dependency)",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"dropped": dropped},
            )
        missing = _missing_config_groups(final_config)
```

`fragment_text` is `fragment_bytes.decode()`, resolved once in `build()` before checkout (see the data-flow note in Step 5); it is in scope in `build()` and is not re-resolved here.

- [ ] **Step 7: Add the `catalog` branch to `_resolve_config_ref` (remote)**

Replace the `LocalComponentRef`-only `_resolve_config_ref` (near line 239) so it also accepts `CatalogComponentRef`, returning bytes (catalog) or a path (local). Inject the catalog fetch into the build object so the resolver stays synchronous and testable:

```python
def _resolve_config_bytes(
    ref: ComponentRef,
    *,
    allowed_component_roots: list[Path],
    catalog_fetch: Callable[[str], bytes],
) -> bytes:
    """Resolve a config ref to fragment bytes: a local file's bytes, or catalog bytes by name."""
    if isinstance(ref, LocalComponentRef):
        path = validate_local_component_path(
            ref.path, allowed_roots=allowed_component_roots, sha256=ref.sha256
        )
        return path.read_bytes()
    if isinstance(ref, CatalogComponentRef):
        return catalog_fetch(ref.name)
    raise _ref_error("config", "config component ref must be local or catalog for builds")
```

`validate_config_ref` (the pre-build validation the MCP tool calls) keeps its shape but now accepts both kinds: a `CatalogComponentRef` validates by confirming `catalog_fetch(ref.name)` resolves (or a lighter existence check).

> **The build-path catalog fetch MUST be synchronous.** `build()` is a sync method run off the event loop via `await asyncio.to_thread(builder.build, ...)` (`jobs/handlers/runs.py:133`), so it cannot `await` the async `get_build_config` (Task 1, line 194). Mirror the build's existing **sync** `store_factory` (`composition.py:108`): add a **synchronous** repository read `get_build_config_sync(conn, name)` in `build_configs/catalog.py` using a sync `psycopg.Connection` (the bootstrap idiom: `psycopg.connect(url)`), and wire `catalog_fetch` in `composition.py` as a plain `Callable[[str], bytes]` that opens a sync connection, calls `get_build_config_sync`, does the build's existing **sync** get — `fetched = store.get_artifact(entry.object_key, None); data = fetched.data` (`get_artifact` returns a `FetchedArtifact(data, ...)`, objectstore.py:194 — extract `.data`, it is not raw bytes) — calls `entry.verify_bytes(data)`, and returns `data`. The async `get_build_config` stays for the MCP tool path (Task 3), which runs on the loop. Add a sync sibling test in `tests/build_configs/test_catalog.py` for `get_build_config_sync`.

- [ ] **Step 8: Mirror Steps 3,5,6,7 in `local_libvirt/build.py`**

`local_libvirt/build.py` has the identical `_stage_config`/`_resolve_config_ref`/`run` shapes (verified: `local_libvirt/build.py:455-479`, `:194`, `:163-169`). Apply the same `_dropped_fragment_symbols`, `_merge_config`, survival check, and `_resolve_config_bytes` changes there. Add the same two unit tests to `tests/providers/local_libvirt/test_build.py`. Keep the two providers' logic textually parallel (the ADR's "lands symmetrically").

> If `_dropped_fragment_symbols`/`_fragment_symbols` would be copy-pasted verbatim into both providers, hoist them into a shared module both import (e.g. `src/kdive/providers/build_common.py` or the existing `providers/debug_common`-style shared package) — DRY. Add a single shared-module test.

- [ ] **Step 9: Make `ServerBuildProfile.config` optional + default**

In `src/kdive/profiles/build.py` change line 70:

```python
    config: ComponentRef | None = None
```

**Substitute at the resolver boundary, not the MCP-tool validation site.** The build does not run in `mcp/tools/lifecycle/runs/build.py` — that path only *validates* at run-creation. The build *executes* asynchronously in a job handler (`jobs/handlers/runs.py` → the provider build), reading the persisted profile's `config`. If the default were applied only at the MCP validation site, the job handler would still hand `config=None` to the provider and the resolver would fail. So apply the default at the single point every build path converges on — the resolve in `build()` (Step 5 data-flow note):

```python
        config_ref = profile.config or CatalogComponentRef(kind="catalog", name="kdump")
        fragment_bytes = _resolve_config_bytes(config_ref, ...)
```

This covers both providers and every caller (job handler, spine, direct). Additionally, in `mcp/tools/lifecycle/runs/build.py` (line 152-158), apply the same `or CatalogComponentRef(kind="catalog", name="kdump")` substitution **before** `config_validator(...)`/`reject_unsupported_component_source(...)` so an omitted config is *validated* against the substituted ref at run-creation (a provider that doesn't support `catalog` rejects early). The two substitutions are the same one-liner at two layers: validation-time (fail fast) and execution-time (the authoritative one). Define the default ref once as a module constant (`_DEFAULT_CONFIG_REF`) shared by both so they cannot drift.

Enumerate and check the build-execution call sites so none bypasses the resolver substitution: `jobs/handlers/runs.py`, `jobs/handlers/runs_shared.py`, and any spine driver path. The resolver-boundary placement is what makes this safe regardless of caller.

- [ ] **Step 10: Admit `catalog` in composition + wire the fetch**

In `src/kdive/providers/composition.py`, both `_local_component_sources()` (line ~108) and `_remote_component_sources()` (line ~216): change `CONFIG_COMPONENT: {"local"}` → `CONFIG_COMPONENT: {"local", "catalog"}`. Construct each provider's build with a **synchronous** `catalog_fetch: Callable[[str], bytes]` (the build runs in a thread — see Step 7): it opens a sync `psycopg.connect(config.require(DATABASE_URL))`, calls `get_build_config_sync(conn, name)` → `CONFIGURATION_ERROR` if `None`, does the build's existing sync get — `fetched = store.get_artifact(entry.object_key, None); data = fetched.data` (extract `.data`; `get_artifact` returns a `FetchedArtifact`, not bytes) — calls `entry.verify_bytes(data)`, and returns `data`. Use the same sync `object_store_from_env()` the build's `store_factory` already uses (composition.py:152). Do not pass an async pool into the build — it cannot await.

- [ ] **Step 11: Write the profile-default + resolver tests**

In `tests/profiles/test_build.py`: a `ServerBuildProfile` document omitting `config` parses (no longer a `configuration_error`), and `parsed.config is None`. In each provider's build test: `_resolve_config_bytes` accepts a `LocalComponentRef` (returns file bytes), accepts a `CatalogComponentRef` (returns the injected catalog bytes), and rejects an `ArtifactComponentRef` with `CONFIGURATION_ERROR`. **Execution-path test (covers Finding 1):** drive the provider `build()` (or the narrowest unit around the resolve step) with a `profile` whose `config is None` and an injected `catalog_fetch`, and assert the kdump fragment bytes are what get merged — i.e. the default fires on the *build* path, not only at MCP validation. In `mcp/tools/lifecycle/runs/build.py`'s test: an omitted config validates the `kdump` catalog ref (assert the substituted ref is `_DEFAULT_CONFIG_REF` == `CatalogComponentRef(name="kdump")`).

- [ ] **Step 12: Fix the integration seed in lockstep**

In `tests/integration/_seed.py:71`, replace:

```python
    "config": {"kind": "local", "path": "/configs/kdump.config"},
```

with the catalog ref (or drop the key entirely to exercise the default):

```python
    "config": {"kind": "catalog", "name": "kdump"},
```

Grep the repo for any other `/configs/kdump.config` or `x86_64-kdump.config` references that feed a *build* (unit fixtures) and switch them too; leave doc/plan mentions for Task 4.

```bash
rg -n "configs/.*kdump.*config|kdump.*config" tests/ src/
```

- [ ] **Step 13: Run guardrails**

```bash
uv run ruff check . && uv run ruff format --check . && uv run ty check
uv run pytest -q tests/providers/remote_libvirt/test_build.py \
  tests/providers/local_libvirt/test_build.py tests/profiles/test_build.py \
  tests/mcp/lifecycle
```
Expected: green. The `live_vm`-gated `_merge_config`/`_real_*` paths stay skipped locally.

- [ ] **Step 14: Commit**

```bash
git add src/kdive/providers src/kdive/profiles tests/providers tests/profiles \
        tests/integration/_seed.py tests/mcp
git commit -m "feat(build): merge a kdump fragment onto defconfig in both providers

Replace full-.config staging with make defconfig + merge_config.sh -m +
single olddefconfig + a fragment-survival check, add a catalog branch to
the config-ref resolver, admit catalog config refs, default an omitted
ServerBuildProfile.config to the kdump catalog entry, and switch the
integration seed off the dead /configs/kdump.config path.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `buildconfig.get` MCP read tool

**Files:**
- Create: `src/kdive/mcp/tools/catalog/build_configs.py`, `tests/mcp/catalog/test_build_configs_tool.py`
- Modify: `src/kdive/mcp/app.py` — import the new module in the `from kdive.mcp.tools.catalog import (...)` block (line ~29) and add `_plain(build_configs.register)` to the registrar list (the same `register(app, pool)` seam every plane uses, line ~80-83). Then regenerate the tool docs.

- [ ] **Step 1: Write the failing tool test**

Create `tests/mcp/catalog/test_build_configs_tool.py`. **Use the real disposable-Postgres + disposable-MinIO fixtures** re-exported from `tests/mcp/conftest.py` (the MCP suite's DB/object-store doubles) — do **not** invent `fake_conn`/`fake_store`. Seed a real row + object first (reuse `seed_build_configs` from Task 1, or insert directly), then call the tool through its connection-pool entry point. Confirm the exact fixture names by reading `tests/mcp/conftest.py` and an existing DB-backed MCP test (e.g. `tests/mcp/lifecycle/test_create_upload_tool.py`); the sketch below names them `db_pool` / `object_store` — replace with the actual fixture names:

```python
import hashlib

import pytest

from kdive.build_configs.seed import seed_build_configs
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.tools.catalog.build_configs import read_build_config


@pytest.mark.asyncio
async def test_buildconfig_get_returns_inline_bytes_and_sha(db_pool, object_store) -> None:
    async with db_pool.connection() as conn:
        await seed_build_configs(conn, object_store)  # publishes the packaged kdump fragment
    data = (
        __import__("kdive.build_configs.seed", fromlist=["KDUMP_FRAGMENT_PATH"])
        .KDUMP_FRAGMENT_PATH.read_bytes()
    )

    async with db_pool.connection() as conn:
        resp = await read_build_config(conn, object_store, name="kdump")

    assert resp.content == data.decode()
    assert resp.sha256 == hashlib.sha256(data).hexdigest()
    assert "merge_config.sh -m" in resp.merge_recipe


@pytest.mark.asyncio
async def test_buildconfig_get_unknown_name_is_configuration_error(db_pool, object_store) -> None:
    async with db_pool.connection() as conn:
        with pytest.raises(CategorizedError) as exc:
            await read_build_config(conn, object_store, name="nope")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest -q tests/mcp/catalog/test_build_configs_tool.py`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the tool**

Create `src/kdive/mcp/tools/catalog/build_configs.py`. Mirror the read-only catalog tool shape in `catalog/artifacts.py`: a `register(app: FastMCP, pool: AsyncConnectionPool) -> None` that registers the tool with `annotations=_docmeta.read_only()` and the catalog-read authorization (`current_context`), plus a testable inner `async def read_build_config(conn, store, *, name) -> ToolResponse`. The inner function:
- `from kdive.build_configs.catalog import get_build_config` (the async repository read; the tool runs on the loop);
- `entry = await get_build_config(conn, name)` → `CONFIGURATION_ERROR` if `None`;
- `fetched = await asyncio.to_thread(store.get_artifact, entry.object_key, None)` — `get_artifact` is a synchronous boto3 call whose docstring requires async callers to offload via `asyncio.to_thread` (objectstore.py:153), and it returns a `FetchedArtifact`, so take `data = fetched.data`;
- `entry.verify_bytes(data)`;
- return a `ToolResponse` carrying `content=data.decode()`, `sha256=entry.sha256`, and `merge_recipe=_MERGE_RECIPE`. The fragment is non-sensitive, so `content` is returned without redaction.

```python
_MERGE_RECIPE = (
    "make defconfig && scripts/kconfig/merge_config.sh -m .config kdump.config "
    "&& make olddefconfig  # then verify every CONFIG_* in kdump.config is present in .config"
)
```

Register it next to the other `catalog/*` read tools, with `read_only()` annotations and the catalog-read role.

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest -q tests/mcp/catalog/test_build_configs_tool.py`
Expected: PASS.

- [ ] **Step 5: Regenerate + verify the tool docs**

```bash
just docs            # regenerate the agent-facing tool reference
just docs-check      # CI gate: committed reference matches a fresh generation
```
Expected: `docs-check` passes after `docs` regenerates.

- [ ] **Step 6: Run guardrails**

```bash
uv run ruff check . && uv run ruff format --check . && uv run ty check
uv run pytest -q tests/mcp/catalog/test_build_configs_tool.py
```

- [ ] **Step 7: Commit**

```bash
git add src/kdive/mcp/tools/catalog/build_configs.py tests/mcp/catalog \
        <regenerated tool-doc file(s)>
git commit -m "feat(mcp): buildconfig.get serves the kdump fragment inline

Read-only catalog tool returning the seeded fragment bytes, its sha256,
and a merge recipe (-m + survival check) backed by the same object-store
artifact the build resolves, so a downloaded fragment matches a built-with
fragment by sha256.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Runbook + residual cleanup

**Files:**
- Create: `docs/runbooks/four-method-live-run.md` (or extend the remote-live-stack runbook).
- Modify: any remaining `/configs/kdump.config` mentions in docs/plans.

- [ ] **Step 1: Write the runbook**

Document the operator steps to validate all four capture methods on the from-source System B: seed verification (`build_config_catalog` row present, `buildconfig.get name=kdump` returns the fragment), a from-source build (no explicit config → kdump default), install + boot, then drive `kdump`, `gdbstub`, `console`, and `host_dump` captures. State that this is the milestone's acceptance gate (operator-run, not CI), consistent with prior milestones. Cross-link from the live-stack hub.

- [ ] **Step 2: Sweep residual dead references**

```bash
rg -n "/configs/kdump.config|x86_64-kdump.config" docs/ src/ tests/
```
Replace any remaining build-feeding references; for doc/plan prose, update to the catalog ref or remove.

- [ ] **Step 3: Guardrails (docs)**

```bash
just check-mermaid   # if the runbook has diagrams
uv run pytest -q -k "docs or runbook" || true
```

- [ ] **Step 4: Commit**

```bash
git add docs/runbooks
git commit -m "docs: four-method live-run runbook for the from-source System

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage:**
- Fragment (spec §1) → Task 1 Step 2. ✓
- Storage & seed, fixed reserved key, idempotency (spec §2) → Task 1 Steps 1,9,11. ✓ (concrete `put_artifact` + reserved `(system, build-configs, kdump)` key, no artifacts-table row.)
- Component sources + resolver + implicit-default schema change (spec §3) → Task 2 Steps 7-11. ✓
- Build flow `defconfig` → `merge_config.sh -m` → single `olddefconfig` → survival check (spec §3) → Task 2 Steps 3,5,6,8. ✓
- Agent retrieval inline + read-only (spec §4) → Task 3. ✓
- Components & seams, error handling categories → Tasks 1-3 (CONFIGURATION_ERROR unknown name / dropped symbol; INFRASTRUCTURE_FAILURE sha mismatch). ✓
- Testing strategy (unit list + integration switch) → Tasks 1-2 tests + Task 2 Step 12. ✓
- Acceptance gate runbook → Task 4. ✓

**Placeholder scan:** The fragment's final symbol set is explicitly deferred to a real `make olddefconfig` (spec open question) and flagged at Task 1 Step 2 — not a hidden TODO. `stored.key` attribute name carries a confirm-note (Task 1 Step 9). No "add error handling"/"similar to"/bare-TODO placeholders.

**Type consistency:** `BuildConfigEntry(name, object_key, sha256, description)` is used identically in Tasks 1 and 3. `get_build_config(conn, name)`, `entry.verify_bytes(data)`, `_dropped_fragment_symbols(fragment_text, final_config_text)`, `CatalogComponentRef(kind="catalog", name=...)` are consistent across tasks. The seed reserved key `system/build-configs/kdump/kdump.config` matches the tool test and resolver. `merge_config.sh -m` is used uniformly in the build flow, the recipe, and the tests.

**Decomposition order:** 1 (additive, no consumer) → 2 (consumes the Task-1 repository; fixes the seed in lockstep so no dead-path window) → 3 (consumes the seeded artifact) ‖ 4 (after 2). Each task ends green and is a single commit.
