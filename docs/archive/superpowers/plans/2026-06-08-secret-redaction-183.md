# Forced Secret Resolution → End-to-End Redaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the fault-inject mock provider a self-contained loop that resolves a high-entropy `secret_ref` under a per-op-unique scope, emits the value into a captured console transcript, redacts-and-persists that transcript through the same registry's `Redactor`, and releases the scope only after persist — proving the mask-before-persist half of the secret contract end to end.

**Architecture:** A new fault-inject-only class `FaultInjectSecretConsole` (in `src/kdive/providers/fault_inject/secret_console.py`) takes injected `SecretBackend`, `SecretRegistry`, an object-store factory, `secret_ref` (an **absolute** path under the allowlisted secrets root — `FileRefBackend.resolve` confines and requires absolute refs, mirroring the existing secret-backend tests), and a per-op scope identity. It resolves the secret (which registers it before return), writes a synthetic console transcript containing the value, builds a `Redactor` from the registry, redacts the transcript, persists the redacted bytes via the store, and *then* releases the scope. Its result carries both the persisted `StoredArtifact` and an explicit redacted-transcript snippet. The generic `Provisioner`/`Connector`/`Retriever` port signatures are unchanged (carried-invariant 1). A spy store injected in tests records the bytes seen at persist time and a registry-snapshot probe asserts the value was still registered at that moment — proving release-after-persist ordering.

**Tech Stack:** Python 3.13, `uv`/`ruff`/`ty`/`pytest`. Reuses `kdive.security.secrets.secrets.FileRefBackend`, `kdive.security.secrets.secret_registry.SecretRegistry`, `kdive.security.secrets.redaction.Redactor`/`REDACTION`, `kdive.store.objectstore.ArtifactWriteRequest`/`StoredArtifact`.

---

## File structure

- **Create** `src/kdive/providers/fault_inject/secret_console.py` — the `FaultInjectSecretConsole` class (the resolve→emit→redact→persist→release loop) plus its `SecretConsoleOutput` result type.
- **Create** `tests/providers/fault_inject/test_secret_console.py` — unit tests driving the loop directly with an injected spy store, an injected `SecretRegistry`, and a `FileRefBackend` over a `tmp_path` secrets root.
- **Modify** `docs/adr/0073-forced-secret-resolution-redaction.md` — already accepted + bound (done in design phase; no further edit).

No migration, no MCP tool, no `ProviderRuntime` field change, no edit to `provider.py` (the seam is its own module to avoid colliding with #182, which edits `provider.py`).

---

## Conventions to honor

- Google-style docstrings on the public class/methods. ≤100 lines/function, ≤100-char lines, absolute imports only.
- The store write request mirrors `FaultInjectRetrieve._put`: `tenant="fault-inject"`, `owner_kind="systems"`, sensitivity on the *redacted* artifact is `Sensitivity.REDACTED`.
- Guardrails: `just lint`, `just type`, `just test` all green before every commit.

---

### Task 1: The redact-and-persist loop with release-after-persist

**Files:**
- Create: `src/kdive/providers/fault_inject/secret_console.py`
- Test: `tests/providers/fault_inject/test_secret_console.py`

- [ ] **Step 1: Write the failing test for the happy-path mask-before-persist loop**

Create `tests/providers/fault_inject/test_secret_console.py`:

```python
"""The fault-inject secret-console loop: resolve -> emit -> redact -> persist -> release (ADR-0073)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from kdive.domain.models import Sensitivity
from kdive.providers.fault_inject.secret_console import (
    FaultInjectSecretConsole,
    SecretConsoleOutput,
)
from kdive.security.secrets.redaction import REDACTION
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend
from kdive.store.objectstore import ArtifactWriteRequest, StoredArtifact

_SENTINEL = "bmc-7f3a9c1e5d2b8a04f6e1c0937a55de28b41f9c6d0e2a7b3"  # 47 hex-ish, high-entropy


class _SpyStore:
    """Records every persisted request's bytes and the registry snapshot at write time."""

    def __init__(self, registry: SecretRegistry) -> None:
        self._registry = registry
        self.requests: list[ArtifactWriteRequest] = []
        self.snapshot_at_write: frozenset[str] | None = None

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.requests.append(request)
        self.snapshot_at_write = self._registry.snapshot()
        return StoredArtifact(
            key=request.key(),
            etag="spy-etag",
            sensitivity=request.sensitivity,
            retention_class=request.retention_class,
        )


def _sentinel_ref(root: Path) -> str:
    """Write the sentinel under the root and return its ABSOLUTE ref (FileRefBackend requires it)."""
    secret = root / "fault-inject" / "console-sentinel"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(_SENTINEL, encoding="utf-8")
    return str(secret)


def _backend(root: Path, registry: SecretRegistry, scope: object) -> FileRefBackend:
    return FileRefBackend(root, registry, scope=scope)


def test_persisted_transcript_is_masked_and_carries_placeholder(tmp_path: Path) -> None:
    registry = SecretRegistry()
    store = _SpyStore(registry)
    system_id = uuid4()
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)
    console = FaultInjectSecretConsole(
        backend=_backend(tmp_path, registry, scope),
        registry=registry,
        store_factory=lambda: store,
        secret_ref=ref,
    )

    output = console.emit_and_persist(system_id=system_id, scope=scope)

    assert isinstance(output, SecretConsoleOutput)
    persisted = store.requests[-1].data.decode("utf-8")
    # mask-before-persist: the persisted bytes lack the raw sentinel and carry the placeholder.
    assert _SENTINEL not in persisted
    assert REDACTION in persisted
    # response snippet (a concrete field, not the StoredArtifact tuple) is masked too.
    assert _SENTINEL not in output.transcript_snippet
    assert REDACTION in output.transcript_snippet
    assert output.artifact.sensitivity is Sensitivity.REDACTED


def test_masking_is_the_registry_exact_value_path_not_the_pattern(tmp_path: Path) -> None:
    # Guard against a spurious pass: a control Redactor built from an EMPTY registry must
    # leave the bare sentinel PRESENT, proving the persisted masking came from the
    # register->exact-value path (ADR-0073), not the Redactor's independent key=value regex.
    from kdive.providers.fault_inject.secret_console import _synthetic_transcript
    from kdive.security.secrets.redaction import Redactor

    control = Redactor(registry=SecretRegistry())  # empty registry, no value seeded
    assert _SENTINEL in control.redact_text(_synthetic_transcript(_SENTINEL))

    seeded = SecretRegistry()
    seeded.register(_SENTINEL, scope="probe")
    assert _SENTINEL not in Redactor(registry=seeded).redact_text(_synthetic_transcript(_SENTINEL))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/providers/fault_inject/test_secret_console.py::test_persisted_transcript_is_masked_and_carries_placeholder -q`
Expected: FAIL — `ModuleNotFoundError: kdive.providers.fault_inject.secret_console`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/kdive/providers/fault_inject/secret_console.py`:

```python
"""The fault-inject forced-secret-resolution loop (ADR-0073, M1.5 issue 4).

A fault-inject-only seam — the generic provider ports stay unchanged. It resolves a
high-entropy ``secret_ref`` through an injected ``SecretBackend`` (which registers the
value before returning it), emits the value into a synthetic console transcript, redacts
that transcript with a ``Redactor`` built from the same registry, persists the redacted
bytes, and releases the per-op scope **only after** the persist — so no resolved value
reaches the object store or the returned snippet unmasked, and none lingers in the
registry past the op.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple, Protocol
from uuid import UUID

from kdive.domain.models import Sensitivity
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend
from kdive.store.objectstore import ArtifactWriteRequest, StoredArtifact

_TENANT = "fault-inject"
_RETENTION_CLASS = "console"
_ARTIFACT_NAME = "console-transcript-redacted"


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


class SecretConsoleOutput(NamedTuple):
    """The persisted redacted artifact plus the redacted transcript snippet a caller surfaces."""

    artifact: StoredArtifact
    transcript_snippet: str


def _synthetic_transcript(value: str) -> str:
    """A console transcript that echoes the resolved credential, as a real console would.

    The value is emitted **bare** (not as ``password=<value>``) so that *only* the
    registry's exact-value masking — not the ``Redactor``'s independent key=value regex —
    can mask it. This keeps the mask-before-persist assertion a real test of the
    register->mask path (ADR-0073), not a coincidence of pattern matching.
    """
    return (
        "fault-inject console boot\n"
        f"[bmc] handshake echoed credential {value} to the console\n"
        "fault-inject console ready\n"
    )


class FaultInjectSecretConsole:
    """Resolve a secret, emit it into a transcript, redact-and-persist, release after persist."""

    def __init__(
        self,
        *,
        backend: SecretBackend,
        registry: SecretRegistry,
        store_factory: Callable[[], _StorePort],
        secret_ref: str,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._store_factory = store_factory
        self._secret_ref = secret_ref

    def emit_and_persist(self, *, system_id: UUID, scope: object) -> SecretConsoleOutput:
        """Run the full loop under ``scope``; release the scope only after the persist.

        Args:
            system_id: The System the synthetic console belongs to (the artifact owner).
            scope: The per-op-unique registry scope identity the backend registered under;
                released here, after redact-and-persist, never before.

        Returns:
            The persisted redacted ``StoredArtifact`` and the redacted transcript snippet.
        """
        try:
            value = self._backend.resolve(self._secret_ref)
            transcript = _synthetic_transcript(value)
            redactor = Redactor(registry=self._registry)
            redacted = redactor.redact_text(transcript)
            artifact = self._store_factory().put_artifact(
                ArtifactWriteRequest(
                    tenant=_TENANT,
                    owner_kind="systems",
                    owner_id=str(system_id),
                    name=_ARTIFACT_NAME,
                    data=redacted.encode("utf-8"),
                    sensitivity=Sensitivity.REDACTED,
                    retention_class=_RETENTION_CLASS,
                )
            )
            return SecretConsoleOutput(artifact=artifact, transcript_snippet=redacted)
        finally:
            self._registry.release(scope)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m pytest tests/providers/fault_inject/test_secret_console.py::test_persisted_transcript_is_masked_and_carries_placeholder -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/fault_inject/secret_console.py tests/providers/fault_inject/test_secret_console.py
git commit -m "feat(fault-inject): add forced secret-resolution console loop"
```
(End the message with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.)

---

### Task 2: Release happens after persist (ordering proof) and value is gone after release

**Files:**
- Test: `tests/providers/fault_inject/test_secret_console.py`

- [ ] **Step 1: Write the failing ordering + post-release tests**

Append to `tests/providers/fault_inject/test_secret_console.py`:

```python
def test_value_is_registered_at_persist_time_and_gone_after(tmp_path: Path) -> None:
    registry = SecretRegistry()
    store = _SpyStore(registry)
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)
    console = FaultInjectSecretConsole(
        backend=_backend(tmp_path, registry, scope),
        registry=registry,
        store_factory=lambda: store,
        secret_ref=ref,
    )

    console.emit_and_persist(system_id=uuid4(), scope=scope)

    # Release-after-persist: the value was still registered at the moment of the write
    # (so the Redactor could mask it), and is evicted only after the loop returns.
    assert store.snapshot_at_write is not None
    assert _SENTINEL in store.snapshot_at_write
    assert _SENTINEL not in registry.snapshot()


def test_scope_is_released_even_when_persist_raises(tmp_path: Path) -> None:
    registry = SecretRegistry()
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)

    class _FailingStore:
        def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
            raise RuntimeError("object store down")

    console = FaultInjectSecretConsole(
        backend=_backend(tmp_path, registry, scope),
        registry=registry,
        store_factory=lambda: _FailingStore(),
        secret_ref=ref,
    )

    try:
        console.emit_and_persist(system_id=uuid4(), scope=scope)
    except RuntimeError:
        pass
    # The finally-release still evicts the value so a failed persist does not leak a
    # global redaction needle that outlives the op.
    assert _SENTINEL not in registry.snapshot()
```

- [ ] **Step 2: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/providers/fault_inject/test_secret_console.py -q`
Expected: PASS (the loop already releases in a `finally` after persist; these tests pin that behavior).

- [ ] **Step 3: Commit**

```bash
git add tests/providers/fault_inject/test_secret_console.py
git commit -m "test(fault-inject): pin release-after-persist ordering and eviction"
```

---

### Task 3: Concurrency — a concurrent op's release does not evict this op's value early

**Files:**
- Test: `tests/providers/fault_inject/test_secret_console.py`

- [ ] **Step 1: Write the failing concurrency test (distinct per-op sentinels)**

Append to `tests/providers/fault_inject/test_secret_console.py`:

```python
def test_concurrent_op_release_does_not_evict_this_ops_value(tmp_path: Path) -> None:
    # Two ops resolve DISTINCT high-entropy sentinels under DISTINCT scopes, so isolation is
    # proven by value-eviction (the registry refcounts by value), not masked by a shared value.
    registry = SecretRegistry()
    value_a = "op-a-9d4f1c7e2b6a8053f1e0c93a7d55be21c40f8b6e"
    value_b = "op-b-3a8c0e5d1f7b9264a0e1c83975dd4be20c41f9a7"
    (tmp_path / "fault-inject").mkdir(parents=True, exist_ok=True)
    file_a = tmp_path / "fault-inject" / "a"
    file_b = tmp_path / "fault-inject" / "b"
    file_a.write_text(value_a, encoding="utf-8")
    file_b.write_text(value_b, encoding="utf-8")
    scope_a, scope_b = "op-a", "op-b"

    FileRefBackend(tmp_path, registry, scope=scope_a).resolve(str(file_a))
    FileRefBackend(tmp_path, registry, scope=scope_b).resolve(str(file_b))
    assert {value_a, value_b} <= registry.snapshot()

    # Op B releases first; op A's value must survive (distinct scope, distinct value).
    registry.release(scope_b)
    assert value_a in registry.snapshot()
    assert value_b not in registry.snapshot()

    registry.release(scope_a)
    assert value_a not in registry.snapshot()
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `uv run python -m pytest tests/providers/fault_inject/test_secret_console.py::test_concurrent_op_release_does_not_evict_this_ops_value -q`
Expected: PASS (the `SecretRegistry` already isolates by scope+value; this pins the ADR's concurrency claim).

- [ ] **Step 3: Commit**

```bash
git add tests/providers/fault_inject/test_secret_console.py
git commit -m "test(fault-inject): prove scope isolation across concurrent ops"
```

---

### Task 4: Wire the loop reachable from composition (no production exposure)

**Files:**
- Modify: `src/kdive/providers/fault_inject/secret_console.py` (add a `from_env`-style factory)
- Test: `tests/providers/fault_inject/test_secret_console.py`

This makes the loop constructible the way the worker boundary would build it — from the real `FileRefBackend` (via `secret_backend_from_env`) bound to a fresh per-op scope and the supplied registry — without adding any default-production exposure (the fault-inject runtime is already opt-in, ADR-0071).

- [ ] **Step 1: Write the failing factory test**

Append to `tests/providers/fault_inject/test_secret_console.py`:

```python
import pytest


def test_for_op_binds_backend_to_the_op_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(tmp_path))
    ref = _sentinel_ref(tmp_path)
    registry = SecretRegistry()
    store = _SpyStore(registry)
    scope = "op-xyz"

    console = FaultInjectSecretConsole.for_op(
        registry=registry,
        store_factory=lambda: store,
        secret_ref=ref,
        scope=scope,
    )
    output = console.emit_and_persist(system_id=uuid4(), scope=scope)

    assert _SENTINEL not in output.transcript_snippet
    assert REDACTION in output.transcript_snippet
    assert _SENTINEL not in registry.snapshot()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/providers/fault_inject/test_secret_console.py::test_for_op_binds_backend_to_the_op_scope -q`
Expected: FAIL — `AttributeError: type object 'FaultInjectSecretConsole' has no attribute 'for_op'`.

- [ ] **Step 3: Add the `for_op` classmethod**

Add to `FaultInjectSecretConsole` in `src/kdive/providers/fault_inject/secret_console.py` (and import `secret_backend_from_env`):

```python
    @classmethod
    def for_op(
        cls,
        *,
        registry: SecretRegistry,
        store_factory: Callable[[], _StorePort],
        secret_ref: str,
        scope: object,
    ) -> FaultInjectSecretConsole:
        """Build the loop with a ``FileRefBackend`` bound to ``registry`` under ``scope``.

        The backend resolves under the allowlisted ``KDIVE_SECRETS_ROOT`` (ADR-0027) and
        registers each resolved value under the per-op ``scope`` the worker boundary owns.
        """
        backend = secret_backend_from_env(registry=registry, scope=scope)
        return cls(
            backend=backend,
            registry=registry,
            store_factory=store_factory,
            secret_ref=secret_ref,
        )
```

Add the import near the top:

```python
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m pytest tests/providers/fault_inject/test_secret_console.py -q`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/fault_inject/secret_console.py tests/providers/fault_inject/test_secret_console.py
git commit -m "feat(fault-inject): add for_op factory binding backend to op scope"
```

---

### Task 5: Full guardrails + quarantine follow-up issue

**Files:** none (verification + issue filing)

- [ ] **Step 1: Run the full local gate**

Run: `just lint && just type && just test`
Expected: all green, zero warnings. Fix any finding before proceeding.

- [ ] **Step 2: File the quarantine-path follow-up issue**

ADR-0073 routes the unimplemented separate-pre-registration-write (object-store quarantine) case to a follow-up, not built here. File it:

```bash
gh issue create \
  --title "Object-store quarantine for pre-registration writes (ADR-0073 follow-up)" \
  --label area:security \
  --body "ADR-0073 §Decision notes the quarantine-before-redaction case: an artifact persisted in a separate write *before* secret registration completes has no value to mask yet, so it should be stored raw + flagged sensitive (quarantined) and redacted on access. M1.5 issue #183 surfaced this as a diagnostic finding and implemented only the in-line masking loop. This issue tracks implementing the object-store quarantine path. Surfaced by #183."
```

- [ ] **Step 3: Note the follow-up issue number in the PR body (done at PR creation).**

---

## Self-review

- **Spec coverage:** §Validation surface "Secret register→redact end-to-end" → Tasks 1-4. Acceptance "persisted artifact AND response snippet both masked + placeholder" → Task 1, with a control test proving the masking is the registry exact-value path (an empty-registry `Redactor` leaves the bare sentinel present) and not the `Redactor`'s independent key=value regex. "value gone after release" → Task 2. "concurrent op's release does not evict early" → Task 3. "release only after redact-and-persist" → Task 2 (ordering probe) + the `finally` placement. Quarantine follow-up → Task 5.
- **Carried-invariant 1 (provider seam unchanged):** no generic port signature touched; `provider.py` untouched (avoids #182 collision).
- **Placeholder scan:** every code step shows full code; no TBD/TODO.
- **Type consistency:** `SecretConsoleOutput(artifact, transcript_snippet)`, `emit_and_persist(system_id=, scope=)`, `for_op(registry=, store_factory=, secret_ref=, scope=)` used identically across tasks. `_StorePort.put_artifact` matches the spy and `FaultInjectRetrieve` store shape.
