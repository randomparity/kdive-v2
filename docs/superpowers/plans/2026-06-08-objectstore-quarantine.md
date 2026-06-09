# Object-store quarantine for pre-registration writes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the object-store quarantine path ADR-0073 deferred — a write that lands before secret registration is stored raw, flagged `quarantined`, excluded from every serve gate, and healed to a redacted sibling before the op releases its scope.

**Architecture:** A new `Sensitivity.QUARANTINED` value (migration `0019` widens the `artifacts_sensitivity_check` constraint) is excluded by the existing `sensitivity = 'redacted'` serve gates. A fault-inject-only loop (`quarantine_console.py`, the test vehicle — local-libvirt resolves no secrets) drives store-raw-quarantined → resolve+register → re-fetch → redact → persist-redacted-sibling → release-after-persist. The confined, size-capped secret read is factored out of `FileRefBackend.resolve` so the loop's unregistered pre-write reuses it.

**Tech Stack:** Python 3.13, `uv`, `pytest`, Postgres (testcontainers), boto3/MinIO object store. Guardrails via `just` recipes (`just lint`, `just type`, `just test`).

**Spec:** `docs/superpowers/specs/2026-06-08-objectstore-quarantine-design.md` · **ADR:** `docs/adr/0075-objectstore-quarantine-pre-registration-writes.md`

---

## File structure

| file | create/modify | responsibility |
|------|---------------|----------------|
| `src/kdive/domain/models.py` | modify | add `Sensitivity.QUARANTINED` |
| `src/kdive/db/schema/0019_artifacts_quarantine_sensitivity.sql` | create | widen the sensitivity CHECK |
| `tests/db/test_migrate.py` | modify | migration admits `quarantined`, rejects out-of-set |
| `tests/mcp/catalog/test_artifacts_tools.py` | modify | serve gates exclude a `quarantined` row |
| `src/kdive/security/secrets/secrets.py` | modify | extract `read_secret_file` + `secrets_root_from_env` |
| `tests/security/secrets/test_secrets.py` | modify | `read_secret_file` reads without registering |
| `src/kdive/providers/fault_inject/quarantine_console.py` | create | the store-raw → heal → release loop |
| `tests/providers/fault_inject/test_quarantine_console.py` | create | loop behavior + edges |

---

## Task 1: Add the `quarantined` sensitivity + migration 0019

**Files:**
- Modify: `src/kdive/domain/models.py:111-115`
- Create: `src/kdive/db/schema/0019_artifacts_quarantine_sensitivity.sql`
- Test: `tests/db/test_migrate.py`

- [ ] **Step 1: Write the failing migration test**

Add to `tests/db/test_migrate.py` (uses the existing `pg_conn` autocommit fixture and `psycopg` import already present):

```python
def test_artifacts_sensitivity_check_admits_quarantined(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    pg_conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class) VALUES ('systems', gen_random_uuid(), 'k', 'e', 'quarantined', "
        "'console')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('systems', gen_random_uuid(), 'k2', 'e', 'bogus', "
            "'console')"
        )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/db/test_migrate.py::test_artifacts_sensitivity_check_admits_quarantined -q`
Expected: FAIL — the first `INSERT` raises `psycopg.errors.CheckViolation` (the constraint still only allows `sensitive`/`redacted`).

- [ ] **Step 3: Create migration 0019**

Create `src/kdive/db/schema/0019_artifacts_quarantine_sensitivity.sql`:

```sql
-- 0019_artifacts_quarantine_sensitivity.sql — M1.5 object-store quarantine (ADR-0075).
-- Additive to 0001 (forward-only, ADR-0015). Widens the artifacts.sensitivity CHECK to admit
-- the `quarantined` value — a raw artifact persisted before secret registration completes,
-- excluded from the redacted-only serve gates and healed to a redacted sibling within the op
-- (ADR-0075) — alongside `sensitive`/`redacted`; mirrors Sensitivity in domain/models.py.
-- Drop-and-recreate keeps the constraint name stable for the SQL<->enum tie.
ALTER TABLE artifacts DROP CONSTRAINT artifacts_sensitivity_check;
ALTER TABLE artifacts ADD CONSTRAINT artifacts_sensitivity_check
    CHECK (sensitivity IN ('sensitive', 'redacted', 'quarantined'));
```

- [ ] **Step 4: Add the enum value**

In `src/kdive/domain/models.py`, change the `Sensitivity` enum:

```python
class Sensitivity(StrEnum):
    """Artifact sensitivity — only a ``redacted`` derivative is response-eligible.

    ``quarantined`` is a raw artifact written before secret registration completed
    (ADR-0075): excluded from every serve gate exactly like ``sensitive``, but marking an
    unfulfilled redaction obligation the op heals to a ``redacted`` sibling before release.
    """

    SENSITIVE = "sensitive"
    REDACTED = "redacted"
    QUARANTINED = "quarantined"
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run python -m pytest tests/db/test_migrate.py::test_artifacts_sensitivity_check_admits_quarantined -q`
Expected: PASS.

- [ ] **Step 6: Run the full migration suite (no regressions)**

Run: `uv run python -m pytest tests/db/test_migrate.py -q`
Expected: PASS — all migration/parity tests still green.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/domain/models.py src/kdive/db/schema/0019_artifacts_quarantine_sensitivity.sql tests/db/test_migrate.py
git commit -m "feat(quarantine): add quarantined sensitivity + migration 0019

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Pin the serve-gate exclusion of a quarantined row

The redacted-only gates already exclude `quarantined` by construction; these tests pin that so a future gate change cannot silently begin serving it. They are enabled by Task 1 (the row `INSERT` needs migration 0019).

**Files:**
- Test: `tests/mcp/catalog/test_artifacts_tools.py`

- [ ] **Step 1: Add a quarantined-row seed helper**

Add near `_seed_system_with_artifacts` in `tests/mcp/catalog/test_artifacts_tools.py`:

```python
async def _seed_quarantined_artifact(pool: AsyncConnectionPool, sys_id: str) -> str:
    """Insert a quarantined artifact owned by an existing System; return its id."""
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('systems', %s, %s, 'e', 'quarantined', 'console') "
            "RETURNING id",
            (sys_id, f"k/systems/{sys_id}/console-quarantined"),
        )
        row = await cur.fetchone()
        assert row is not None
        return str(row["id"])
```

- [ ] **Step 2: Write the failing tests**

Add these three tests to the same file:

```python
def test_artifacts_get_excludes_quarantined(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, red_id = await _seed_system_with_artifacts(pool)
            quar_id = await _seed_quarantined_artifact(pool, sys_id)
            quar_resp = await artifacts_get(pool, _ctx(), artifact_id=quar_id)
            red_resp = await artifacts_get(pool, _ctx(), artifact_id=red_id)
        # Positive control: a redacted artifact in the same DB state IS served, so the
        # quarantined error is specifically the sensitivity gate, not a not-found/authz miss.
        assert red_resp.status == "available"
        assert quar_resp.status == "error"
        assert quar_resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_artifacts_list_excludes_quarantined(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, red_id = await _seed_system_with_artifacts(pool)
            quar_id = await _seed_quarantined_artifact(pool, sys_id)
            resp = await artifacts_list(pool, _ctx(), system_id=sys_id)
        ids = {r.object_id for r in resp.items}
        assert quar_id not in ids
        assert red_id in ids

    asyncio.run(_run())


def test_artifacts_search_text_quarantined_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id, _, _ = await _seed_system_with_artifacts(pool)
            quar_id = await _seed_quarantined_artifact(pool, sys_id)
            store = _SearchStore(b"panic")
            resp = await _artifact_read_handlers(store).artifacts_search_text(
                pool, _ctx(), request=_search_request(quar_id, "panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert store.got is False  # excluded by SQL before any object fetch

    asyncio.run(_run())
```

- [ ] **Step 3: Run the tests**

Run: `uv run python -m pytest tests/mcp/catalog/test_artifacts_tools.py -q -k quarantined`
Expected: PASS — the `sensitivity = 'redacted'` gates exclude the quarantined row at the SQL layer (`store.got is False` confirms search never reaches the object). If Docker is absent these skip; that is expected.

- [ ] **Step 4: Commit**

```bash
git add tests/mcp/catalog/test_artifacts_tools.py
git commit -m "test(quarantine): pin serve-gate exclusion of a quarantined row

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Factor out `read_secret_file` (unregistered read)

**Files:**
- Modify: `src/kdive/security/secrets/secrets.py`
- Test: `tests/security/secrets/test_secrets.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/security/secrets/test_secrets.py` (import `read_secret_file` and `SecretRegistry`):

```python
def test_read_secret_file_returns_value_without_registering(tmp_path: Path) -> None:
    from kdive.security.secrets.secret_registry import SecretRegistry
    from kdive.security.secrets.secrets import read_secret_file

    secret = tmp_path / "cred"
    secret.write_text("s3kret-value\n", encoding="utf-8")
    registry = SecretRegistry()

    value = read_secret_file(tmp_path, str(secret))

    assert value == "s3kret-value"  # trailing newline stripped
    assert "s3kret-value" not in registry.snapshot()  # read does NOT register
```

(If `Path` and `SecretRegistry` are already imported at module top, drop the inline imports.)

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/security/secrets/test_secrets.py::test_read_secret_file_returns_value_without_registering -q`
Expected: FAIL — `ImportError: cannot import name 'read_secret_file'`.

- [ ] **Step 3: Extract the read**

In `src/kdive/security/secrets/secrets.py`, replace the body of `FileRefBackend.resolve` and add the two module-level functions. The confinement + 64 KiB cap + newline strip move into `read_secret_file`; `resolve` becomes read-then-register; `secret_backend_from_env` uses the shared root resolver:

```python
def read_secret_file(root: Path, ref: str) -> str:
    """Read a secret file's value, confined to ``root``, size-capped, newline-stripped.

    Does **not** register the value. ``FileRefBackend.resolve`` registers after this read;
    callers that need the raw value without registration (the ADR-0075 quarantine pre-write)
    call this directly.

    Raises:
        PathSafetyError: ``ref`` escapes ``root``, does not exist, or exceeds the cap.
    """
    resolved = confine_to_root(Path(ref), allowed_root=root)
    if not resolved.is_file():
        raise PathSafetyError("secret file does not exist")
    if resolved.stat().st_size > _MAX_SECRET_FILE_BYTES:
        raise PathSafetyError("secret file exceeds the maximum secret size")
    value = resolved.read_text(encoding="utf-8")
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith("\n"):
        return value[:-1]
    return value


def secrets_root_from_env() -> Path:
    """Return the allowlisted secrets root from ``KDIVE_SECRETS_ROOT`` (or the default)."""
    return Path(os.environ.get(_SECRETS_ROOT_ENV, _DEFAULT_SECRETS_ROOT))
```

Change `FileRefBackend.resolve` to:

```python
    def resolve(self, ref: str) -> str:
        value = read_secret_file(self._root, ref)
        self._registry.register(value, scope=self._scope)
        return value
```

Change `secret_backend_from_env` to use the shared resolver:

```python
def secret_backend_from_env(
    *, registry: SecretRegistry | None = None, scope: object | None = None
) -> FileRefBackend:
    """Build the file-ref secret backend from ``KDIVE_SECRETS_ROOT``.

    Resolves credentials only within the allowlisted secrets root and registers each resolved
    value into ``registry`` under ``scope``. Passing ``scope=None`` keeps the original
    process-lifetime scope. Opens no file at construction — the root is read on the first
    ``resolve``.
    """
    return FileRefBackend(secrets_root_from_env(), registry=registry, scope=scope)
```

- [ ] **Step 4: Run the new test + the existing secrets suite**

Run: `uv run python -m pytest tests/security/secrets/test_secrets.py -q`
Expected: PASS — the new test passes and the existing `FileRefBackend.resolve` / `secret_backend_from_env` tests stay green (behavior-preserving).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/security/secrets/secrets.py tests/security/secrets/test_secrets.py
git commit -m "refactor(secrets): extract read_secret_file for an unregistered read

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: The fault-inject quarantine loop

**Files:**
- Create: `src/kdive/providers/fault_inject/quarantine_console.py`
- Test: `tests/providers/fault_inject/test_quarantine_console.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/providers/fault_inject/test_quarantine_console.py`:

```python
"""The fault-inject quarantine loop: store-raw-quarantined -> resolve -> heal -> release (ADR-0075)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from kdive.domain.models import Sensitivity
from kdive.providers.fault_inject.quarantine_console import (
    FaultInjectQuarantineConsole,
    QuarantineHealOutput,
    _quarantined_transcript,
)
from kdive.security.secrets.redaction import REDACTION, Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend
from kdive.store.objectstore import ArtifactWriteRequest, FetchedArtifact, StoredArtifact

_SENTINEL = "quar-1a2b3c4d5e6f70819273645566778899aabbccddeeff0011"  # high-entropy, unique


class _SpyStore:
    """In-memory put/get store; records each put and the registry snapshot at write time."""

    def __init__(self, registry: SecretRegistry) -> None:
        self._registry = registry
        self.requests: list[ArtifactWriteRequest] = []
        self.snapshot_at_put: list[frozenset[str]] = []
        self._objects: dict[str, ArtifactWriteRequest] = {}

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.requests.append(request)
        self.snapshot_at_put.append(self._registry.snapshot())
        self._objects[request.key()] = request
        return StoredArtifact(
            key=request.key(),
            etag="spy-etag",
            sensitivity=request.sensitivity,
            retention_class=request.retention_class,
        )

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        request = self._objects[key]
        return FetchedArtifact(request.data, request.sensitivity, request.retention_class)


def _sentinel_ref(root: Path) -> str:
    secret = root / "fault-inject" / "quarantine-sentinel"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(_SENTINEL, encoding="utf-8")
    return str(secret)


def _console(root: Path, registry: SecretRegistry, store: _SpyStore, scope: object, ref: str):
    return FaultInjectQuarantineConsole(
        backend=FileRefBackend(root, registry, scope=scope),
        registry=registry,
        store_factory=lambda: store,
        secret_ref=ref,
        secrets_root=root,
        scope=scope,
    )


def test_quarantined_raw_retained_and_sibling_is_healed(tmp_path: Path) -> None:
    registry = SecretRegistry()
    store = _SpyStore(registry)
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)

    output = _console(tmp_path, registry, store, scope, ref).emit_and_persist(system_id=uuid4())

    assert isinstance(output, QuarantineHealOutput)
    # The pre-registration write is raw and flagged quarantined.
    quar = output.quarantined
    assert quar.sensitivity is Sensitivity.QUARANTINED
    quar_bytes = store._objects[quar.key].data.decode("utf-8")
    assert _SENTINEL in quar_bytes  # raw: the quarantined object still contains the secret
    # The healed sibling is redacted and masked.
    healed = output.healed
    assert healed.sensitivity is Sensitivity.REDACTED
    assert healed.key != quar.key
    healed_bytes = store._objects[healed.key].data.decode("utf-8")
    assert _SENTINEL not in healed_bytes
    assert REDACTION in healed_bytes
    # The returned snippet (a concrete field, not the StoredArtifact tuple) is masked too.
    assert _SENTINEL not in output.transcript_snippet
    assert REDACTION in output.transcript_snippet


def test_masking_is_the_registry_exact_value_path_not_the_pattern(tmp_path: Path) -> None:
    control = Redactor(registry=SecretRegistry())  # empty registry, no value seeded
    assert _SENTINEL in control.redact_text(_quarantined_transcript(_SENTINEL))

    seeded = SecretRegistry()
    seeded.register(_SENTINEL, scope="probe")
    assert _SENTINEL not in Redactor(registry=seeded).redact_text(_quarantined_transcript(_SENTINEL))


def test_value_registered_at_heal_write_and_gone_after(tmp_path: Path) -> None:
    registry = SecretRegistry()
    store = _SpyStore(registry)
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)

    _console(tmp_path, registry, store, scope, ref).emit_and_persist(system_id=uuid4())

    # Two puts: [0] the quarantined pre-registration write (value NOT yet registered),
    # [1] the healed sibling (value registered, so the Redactor masked it).
    assert _SENTINEL not in store.snapshot_at_put[0]
    assert _SENTINEL in store.snapshot_at_put[1]
    # Released after persist: gone from the registry once the loop returns.
    assert _SENTINEL not in registry.snapshot()


def test_scope_released_even_when_heal_persist_raises(tmp_path: Path) -> None:
    registry = SecretRegistry()
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)

    class _FailOnHealStore:
        def __init__(self) -> None:
            self._puts = 0
            self._objects: dict[str, ArtifactWriteRequest] = {}

        def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
            self._puts += 1
            if self._puts == 2:  # fail the heal write, not the quarantine write
                raise RuntimeError("object store down")
            self._objects[request.key()] = request
            return StoredArtifact(request.key(), "e", request.sensitivity, request.retention_class)

        def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
            request = self._objects[key]
            return FetchedArtifact(request.data, request.sensitivity, request.retention_class)

    console = FaultInjectQuarantineConsole(
        backend=FileRefBackend(tmp_path, registry, scope=scope),
        registry=registry,
        store_factory=_FailOnHealStore,
        secret_ref=ref,
        secrets_root=tmp_path,
        scope=scope,
    )

    with pytest.raises(RuntimeError):
        console.emit_and_persist(system_id=uuid4())
    assert _SENTINEL not in registry.snapshot()  # finally-release evicts despite the failure


def test_concurrent_op_release_does_not_evict_this_ops_value(tmp_path: Path) -> None:
    # Distinct sentinels under distinct scopes prove isolation by scope, not shared-value
    # refcounting (ADR-0073 implementation binding, carried to the quarantine heal).
    registry = SecretRegistry()
    value_a = "quar-a-44ffe1c7b2a8053f1e0c93a7d55be21c40f8b6e9"
    value_b = "quar-b-77aac0e5d1f7b9264a0e1c83975dd4be20c41f9a7"
    (tmp_path / "fault-inject").mkdir(parents=True, exist_ok=True)
    file_a = tmp_path / "fault-inject" / "a"
    file_b = tmp_path / "fault-inject" / "b"
    file_a.write_text(value_a, encoding="utf-8")
    file_b.write_text(value_b, encoding="utf-8")
    scope_a, scope_b = "op-a", "op-b"

    FileRefBackend(tmp_path, registry, scope=scope_a).resolve(str(file_a))
    FileRefBackend(tmp_path, registry, scope=scope_b).resolve(str(file_b))
    assert {value_a, value_b} <= registry.snapshot()

    registry.release(scope_b)  # op B heals + releases first
    assert value_a in registry.snapshot()  # op A's value survives for op A's own heal
    assert value_b not in registry.snapshot()

    registry.release(scope_a)
    assert value_a not in registry.snapshot()


def test_for_op_binds_backend_to_the_op_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(tmp_path))
    ref = _sentinel_ref(tmp_path)
    registry = SecretRegistry()
    store = _SpyStore(registry)
    scope = "op-xyz"

    console = FaultInjectQuarantineConsole.for_op(
        registry=registry,
        store_factory=lambda: store,
        secret_ref=ref,
        scope=scope,
    )
    output = console.emit_and_persist(system_id=uuid4())

    assert output.healed.sensitivity is Sensitivity.REDACTED
    assert _SENTINEL not in output.transcript_snippet
    assert REDACTION in output.transcript_snippet
    assert _SENTINEL not in registry.snapshot()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m pytest tests/providers/fault_inject/test_quarantine_console.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdive.providers.fault_inject.quarantine_console'`.

- [ ] **Step 3: Implement the loop**

Create `src/kdive/providers/fault_inject/quarantine_console.py`:

```python
"""The fault-inject object-store quarantine loop (ADR-0075, M1.5 issue #190 follow-up).

A fault-inject-only seam — the generic provider ports stay unchanged. It models a write that
lands **before** secret registration completes: it reads a high-entropy ``secret_ref`` **raw**
(unregistered), emits it into a synthetic console transcript, and persists that transcript raw
and flagged ``QUARANTINED``. It then resolves the same ref through an injected ``SecretBackend``
(which registers the value before returning it), re-fetches the quarantined object from the
store, redacts it with a ``Redactor`` over the same registry, and persists a ``REDACTED``
sibling — healing the quarantine. The per-op scope is released only **after** the heal persist,
so the value is registered at the moment the heal masks it and never lingers past the op. The
quarantined raw object is retained for provenance; the redacted-only serve gates keep it
unservable.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, Protocol
from uuid import UUID

from kdive.domain.models import Sensitivity
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import (
    SecretBackend,
    read_secret_file,
    secret_backend_from_env,
    secrets_root_from_env,
)
from kdive.store.objectstore import ArtifactWriteRequest, FetchedArtifact, StoredArtifact

_TENANT = "fault-inject"
_RETENTION_CLASS = "console"
_QUARANTINED_NAME = "console-quarantined"
_HEALED_NAME = "console-quarantined-redacted"


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...
    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...


class QuarantineHealOutput(NamedTuple):
    """The retained quarantined raw, the healed redacted sibling, and the masked snippet."""

    quarantined: StoredArtifact
    healed: StoredArtifact
    transcript_snippet: str


def _quarantined_transcript(value: str) -> str:
    """Return a console transcript that echoes the credential **bare**, as a real console would.

    Emitting it bare (not ``password=<value>``) keeps the heal's mask a real test of the
    register->exact-value path (ADR-0075), not a coincidence of the Redactor's key=value regex.
    """
    return (
        "fault-inject console boot\n"
        f"[bmc] handshake echoed credential {value} to the console\n"
        "fault-inject console ready\n"
    )


class FaultInjectQuarantineConsole:
    """Store-raw-quarantined, resolve, re-fetch, heal to a redacted sibling, release after persist."""

    def __init__(
        self,
        *,
        backend: SecretBackend,
        registry: SecretRegistry,
        store_factory: Callable[[], _StorePort],
        secret_ref: str,
        secrets_root: Path,
        scope: object,
    ) -> None:
        """Build the loop.

        Args:
            backend: The secret backend, bound to ``registry`` under ``scope`` so the value it
                resolves is registered under the same scope this loop releases.
            registry: The registry the backend registers into and the ``Redactor`` masks from.
            store_factory: Builds the object store the transcripts are persisted to / fetched from.
            secret_ref: The absolute path of the secret under the allowlisted secrets root.
            secrets_root: The allowlisted root the unregistered pre-write read is confined to.
            scope: The per-op-unique registry scope identity, single-sourced here — the backend
                registers under it and the loop releases it, so they cannot diverge.
        """
        self._backend = backend
        self._registry = registry
        self._store_factory = store_factory
        self._secret_ref = secret_ref
        self._secrets_root = secrets_root
        self._scope = scope

    @classmethod
    def for_op(
        cls,
        *,
        registry: SecretRegistry,
        store_factory: Callable[[], _StorePort],
        secret_ref: str,
        scope: object,
    ) -> FaultInjectQuarantineConsole:
        """Build the loop with a ``FileRefBackend`` and root from ``KDIVE_SECRETS_ROOT`` (ADR-0027)."""
        root = secrets_root_from_env()
        backend = secret_backend_from_env(registry=registry, scope=scope)
        return cls(
            backend=backend,
            registry=registry,
            store_factory=store_factory,
            secret_ref=secret_ref,
            secrets_root=root,
            scope=scope,
        )

    def emit_and_persist(self, *, system_id: UUID) -> QuarantineHealOutput:
        """Run the quarantine loop; release the op's scope only after the heal persist.

        Reads the secret raw (unregistered), persists it raw + ``QUARANTINED`` (the
        pre-registration write), resolves the ref (registering the value under the op scope),
        re-fetches the quarantined object, redacts it, persists a ``REDACTED`` sibling, and
        releases the scope **only after** that persist — so the value is registered when the
        heal masks it and is evicted afterward; a failed heal still releases the scope.

        Args:
            system_id: The System the synthetic console belongs to (the artifact owner).

        Returns:
            The retained quarantined raw ``StoredArtifact``, the healed redacted sibling, and the
            redacted transcript snippet a caller would surface.
        """
        store = self._store_factory()
        try:
            raw_value = read_secret_file(self._secrets_root, self._secret_ref)
            quarantined = store.put_artifact(
                ArtifactWriteRequest(
                    tenant=_TENANT,
                    owner_kind="systems",
                    owner_id=str(system_id),
                    name=_QUARANTINED_NAME,
                    data=_quarantined_transcript(raw_value).encode("utf-8"),
                    sensitivity=Sensitivity.QUARANTINED,
                    retention_class=_RETENTION_CLASS,
                )
            )
            self._backend.resolve(self._secret_ref)
            fetched = store.get_artifact(quarantined.key, quarantined.etag)
            redacted = Redactor(registry=self._registry).redact_text(
                fetched.data.decode("utf-8")
            )
            healed = store.put_artifact(
                ArtifactWriteRequest(
                    tenant=_TENANT,
                    owner_kind="systems",
                    owner_id=str(system_id),
                    name=_HEALED_NAME,
                    data=redacted.encode("utf-8"),
                    sensitivity=Sensitivity.REDACTED,
                    retention_class=_RETENTION_CLASS,
                )
            )
            return QuarantineHealOutput(
                quarantined=quarantined, healed=healed, transcript_snippet=redacted
            )
        finally:
            self._registry.release(self._scope)
```

- [ ] **Step 4: Run the loop tests**

Run: `uv run python -m pytest tests/providers/fault_inject/test_quarantine_console.py -q`
Expected: PASS — all six tests green.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/fault_inject/quarantine_console.py tests/providers/fault_inject/test_quarantine_console.py
git commit -m "feat(quarantine): fault-inject store-raw-quarantine then heal loop

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Full guardrails

- [ ] **Step 1: Run the full local gate**

Run: `just ci`
Expected: lint, type, lint-shell, lint-workflows, check-mermaid, and tests all PASS with zero warnings. Fix every warning before proceeding; if any check fails, fix and re-run from a clean state.

- [ ] **Step 2: Commit any fixes**

```bash
git add -A
git commit -m "chore(quarantine): satisfy guardrails

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

(Skip if `just ci` was already clean with nothing to commit.)

---

## Self-review notes (resolved during planning)

- **Spec coverage:** `QUARANTINED` + migration (Task 1), never-served-clean serve-gate exclusion + migration's load-bearing test (Tasks 1–2), the store-raw → resolve → heal → release loop with all listed edges incl. concurrent-op isolation and release-on-failure (Task 4), the `read_secret_file` refactor (Task 3). Every spec §"Failure modes and edges" bullet maps to a named test.
- **Type consistency:** `FaultInjectQuarantineConsole`, `QuarantineHealOutput(quarantined, healed, transcript_snippet)`, `_quarantined_transcript`, `read_secret_file(root, ref)`, `secrets_root_from_env()` are used identically across the implementation and tests.
- **Gate skips:** the DB tests (Tasks 1–2) skip without Docker; that is the repo's convention, not a failure. CI sets `KDIVE_REQUIRE_DOCKER=1`.
