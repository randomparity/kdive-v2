# Safety modules & file-ref secret backend — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement this plan task-by-task — write the failing test first, then the implementation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the redaction, secret-registry, and path-safety primitives from the PoC into the existing `kdive.security` package, and add a new `secrets.py` with the `SecretBackend` Protocol and a `FileRefBackend` that resolves a file reference only within an allowlisted root, registering the resolved value into the redaction registry before returning it (issue #25).

**Architecture:** Four leaf modules under `src/kdive/security/` with no dependency on the rest of the codebase (no domain models, no MCP). Dependency order: `secret_registry` (no internal deps) → `redaction` (imports `secret_registry`) and `paths` (no internal deps) → `secrets` (imports `paths` + `secret_registry`). `PROCESS_SECRET_REGISTRY` is the single process-global instance both the logging filter and every `Redactor` seed from. `FileRefBackend.resolve` registers before return as a structural invariant.

**Tech Stack:** Python 3.13, stdlib only for these modules (`re`, `logging`, `threading`, `pathlib`, `urllib.parse`, `typing.Protocol`), pytest with real `tmp_path` for the filesystem boundary. Guardrails: `uv run ruff check`, `uv run ruff format`, `uv run ty check src`, `uv run python -m pytest -q`.

**Design source:** [`docs/superpowers/specs/2026-06-04-safety-secret-backend-design.md`](../specs/2026-06-04-safety-secret-backend-design.md) · [`docs/adr/0027-safety-modules-secret-backend-impl.md`](../../adr/0027-safety-modules-secret-backend-impl.md) · [`docs/adr/0012-secret-backend.md`](../../adr/0012-secret-backend.md)

**Reference patterns (read before starting):** PoC sources `~/src/kdive-v1/src/kdive/safety/{redaction,secret_registry,paths}.py` (the verbatim ports for the first three modules — copy semantics, drop the v1-only imports), `src/kdive/security/rbac.py` (module docstring + `from __future__ import annotations` + `StrEnum`/`Protocol` house style), `tests/security/test_rbac.py` (test layout, no-MCP direct-call idiom).

---

## File Structure

- **Create** `src/kdive/security/secret_registry.py` — `SecretRegistry` + `PROCESS_SECRET_REGISTRY` (verbatim port).
- **Create** `src/kdive/security/redaction.py` — `REDACTION`, `redact_url_credentials`, `Redactor`, `SecretRedactionFilter` (verbatim port; fix the import path to `kdive.security.secret_registry`).
- **Create** `src/kdive/security/paths.py` — `PathSafetyError` + `confine_to_root` (scoped port — only this primitive, not the v1 validators).
- **Create** `src/kdive/security/secrets.py` — `SecretBackend` Protocol + `FileRefBackend` (new).
- **Create** `tests/security/test_secret_registry.py`
- **Create** `tests/security/test_redaction.py`
- **Create** `tests/security/test_paths.py`
- **Create** `tests/security/test_secrets.py`

No existing file is modified — `rbac.py`/`audit.py`/`gate.py`/`__init__.py` are untouched (additive members only), which keeps sibling-conflict risk low.

---

## Task 1: Port `secret_registry.py`

**Files:** Create `src/kdive/security/secret_registry.py`; Create `tests/security/test_secret_registry.py`

- [ ] **Step 1: Write the failing tests** (`tests/security/test_secret_registry.py`):
  - `register` then `snapshot` contains the value; empty/`None` is a no-op (snapshot unchanged) and does **not** bump `version`.
  - `register(scope=None)` then `release(None)` keeps the value (global never evicted).
  - `register(scope=obj)` then `release(obj)` removes the value; a value registered under two scopes survives release of one (refcount).
  - `version` is monotonic across non-empty registers and value-bearing releases; unchanged on no-op register and on `release` of an unknown scope.
- [ ] **Step 2: Implement** by porting the v1 module verbatim. The only review delta vs v1: confirm a no-op (empty/`None`) `register` does not increment `_version` (v1 already returns early before the bump — verify and pin with the test above).
- [ ] **Step 3: Guardrails green** — `ruff check`, `ruff format`, `ty check src`, `pytest -q tests/security/test_secret_registry.py`.

## Task 2: Port `redaction.py`

**Files:** Create `src/kdive/security/redaction.py`; Create `tests/security/test_redaction.py`

Depends on Task 1 (imports `secret_registry`).

- [ ] **Step 1: Write the failing tests** (`tests/security/test_redaction.py`):
  - `redact_url_credentials`: `https://u:p@h/x` → `https://h/x`; schemeless `u:p@h/x` stripped; a clean URL with a real netloc and a `:...@` in the *path* is not mangled; a value that raises on parse → `REDACTION`.
  - `Redactor` seeded with an explicit secret value masks it by exact replacement, including a value containing regex metacharacters (e.g. `a.b*c+`), proving `str.replace` not regex is used for values.
  - `Redactor` masks `password=hunter2` / `token: abc` key-value pairs; recurses into nested dict/list/tuple; masks a `{"sensitive": true, "path": ...}` mapping's `path`.
  - `Redactor()` (no explicit values) seeds from `PROCESS_SECRET_REGISTRY` — register a value into a **local** registry is not enough; this test registers into the process-global, asserts masking, then releases. (Use a unique sentinel value and clean up to avoid cross-test pollution.)
  - `SecretRedactionFilter`: a record logged after a new registration is masked; the cached `Redactor` rebuilds only when `version()` changes (spy on `version`/construction or assert behavior across two registrations).
- [ ] **Step 2: Implement** by porting v1 verbatim, changing the import from `kdive.safety.secret_registry` to `kdive.security.secret_registry`. Keep the `# bad %-formatting must never break logging` broad-except (it is justified — a logging filter must never raise; if ruff `BLE001`/`B902` flags it, add a scoped `# noqa` with that justification, or narrow to `Exception` which v1 already uses).
- [ ] **Step 3: Guardrails green** for `tests/security/test_redaction.py`.

## Task 3: Port `paths.py` (scoped to `confine_to_root`)

**Files:** Create `src/kdive/security/paths.py`; Create `tests/security/test_paths.py`

No internal deps. Port **only** `PathSafetyError`, the shell-metachar/control-char set, the `_is_relative_to` helper, and a single `confine_to_root(path, *, allowed_root)`. Do **not** port the v1 validators (`validate_artifact_root`, `validate_run_id`, `validate_secret_file_reference`, `confine_run_relative`, `confine_to_roots`, etc.) — they depend on `SecretReference`/`read_elf_build_id` which do not exist in v2 (ADR-0027 §4).

`confine_to_root` body (adapted from v1 `confine_to_roots`, single-root, no shell-injection-for-overlay framing):
```python
def confine_to_root(path: Path, *, allowed_root: Path) -> Path:
    text = str(path)
    if any(c in _SHELL_METACHARS for c in text) or any(ord(c) < 32 for c in text):
        raise PathSafetyError("secret file reference contains unsafe characters")
    resolved = path.expanduser().resolve()
    if _is_relative_to(resolved, allowed_root.expanduser().resolve()):
        return resolved
    raise PathSafetyError(f"secret file reference escapes the allowed root: {path!r}")
```

- [ ] **Step 1: Write the failing tests** (`tests/security/test_paths.py`, using `tmp_path`):
  - a file directly under `root` resolves and returns the resolved path.
  - a relative `../escape` ref resolves outside `root` → `PathSafetyError`.
  - an absolute path outside `root` → `PathSafetyError`.
  - a symlink **under** `root` whose target is a file **outside** `root` → `PathSafetyError` (containment catches the followed symlink).
  - a symlink **under** `root` whose target is a **non-existent** path **outside** `root` → `PathSafetyError` (pins the `resolve(strict=False)` lexical branch, where the dangling symlink still resolves to the outside target and is caught by containment).
  - a path containing a shell metachar (e.g. `;`) or a control char → `PathSafetyError`, with no resolution attempted reaching the fs.
  - a not-yet-existing tail under `root` is admitted (returns a path under root) — existence is the backend's concern, not `confine_to_root`'s.
- [ ] **Step 2: Implement** the scoped module.
- [ ] **Step 3: Guardrails green** for `tests/security/test_paths.py`.

## Task 4: Add `secrets.py` (`SecretBackend` + `FileRefBackend`)

**Files:** Create `src/kdive/security/secrets.py`; Create `tests/security/test_secrets.py`

Depends on Tasks 1 + 3.

- [ ] **Step 1: Write the failing tests** (`tests/security/test_secrets.py`, using `tmp_path` + a **local** `SecretRegistry`):
  - **Acceptance — exact-value masking:** write a secret file under `root`; `resolve` returns its content; a fresh `Redactor(list(registry.snapshot()))` masks the value in sample output by exact replacement.
  - **Acceptance — escape rejected:** a ref pointing outside `root` (relative `..` and absolute) → `PathSafetyError`, and the target file is **not** read (assert via a sentinel file whose content would be detectable, or by asserting the registry snapshot stays empty).
  - **Acceptance — register-before-return (no-skip):** the observable security property is that after `resolve` returns, the value is already in the registry, so any consumer that builds a `Redactor` next will mask it. Assert this directly on the **real local registry**: after `resolve` returns `V`, `assert V in registry.snapshot()`, then `Redactor(list(registry.snapshot())).redact_text(sample containing V)` masks `V`. (Do **not** build a recording subclass that overrides `register` without populating the store — it would make `snapshot()` empty and the masking assertion would contradict itself. The post-condition on the real registry is the stronger, non-contradictory proof.)
  - **Empty-value drop:** a ref to an empty (and a single-`\n`) file → `resolve` returns `""`; `registry.snapshot()` gains no empty string.
  - **Terminator strip:** a file whose content is `secret\n` resolves to `secret`; a file whose content is `secret\r\n` also resolves to `secret` (not `secret\r`); a file `secret \n` (trailing space before newline) resolves to `secret ` — the significant trailing space is preserved, only the one terminator is stripped.
  - **Nonexistent file** under `root` → `PathSafetyError` (`secret file does not exist`); no value registered.
  - **`registry=None` default** resolves to `PROCESS_SECRET_REGISTRY` (construct with default, assert `backend._registry is PROCESS_SECRET_REGISTRY`); the value-masking tests pass an explicit local registry to avoid global pollution.
  - **`scope` plumbed through:** a value resolved with `scope=obj` is evicted from the registry after `registry.release(obj)`.
- [ ] **Step 2: Implement** `SecretBackend(Protocol)` (`resolve(self, ref: str) -> str`) and `FileRefBackend`:
  - `__init__(self, root, registry=None, *, scope=None)` → store `root`, `self._registry = registry if registry is not None else PROCESS_SECRET_REGISTRY`, `self._scope = scope`.
  - `resolve(ref)` → `resolved = confine_to_root(Path(ref), allowed_root=self._root)`; `if not resolved.is_file(): raise PathSafetyError("secret file does not exist")`; `value = resolved.read_text(encoding="utf-8")`; strip a single trailing line terminator — `\r\n` if present, else a single `\n` — and **nothing else** (not `rstrip`, which would corrupt a credential ending in significant whitespace; but `\r\n` is the common editor/tooling terminator and must not survive into the value, or the un-suffixed logical secret a subprocess emits would escape exact-value redaction). Concretely: `value = value[:-2] if value.endswith("\r\n") else value[:-1] if value.endswith("\n") else value`. Then `self._registry.register(value, scope=self._scope)`; `return value`.
  - Registration is the final statement before `return`; no other return path exists.
- [ ] **Step 3: Guardrails green** for `tests/security/test_secrets.py`, then the whole suite `pytest -q`.

## Task 5: Full-suite guardrails + sanity

- [ ] `uv run ruff check` (zero warnings), `uv run ruff format --check`, `uv run ty check src` (hard-gating in CI), `uv run python -m pytest -q` (whole suite green; env-gated libvirt/gdb/drgn integration tests stay gated/skipped).
- [ ] Confirm no existing file under `src/kdive/security/` was modified (`git status` shows only new files).

---

## Rollback / cleanup

Every task creates new files only; no existing file is edited and no migration or external state is touched. Rollback of any task is `git restore --staged --worktree` of that task's new files (or `git reset --hard` to the prior commit). There is no partial-failure state to compensate — the modules are pure and import-only, with no process-global side effect at import time beyond constructing the empty `PROCESS_SECRET_REGISTRY` singleton (which is the intended, idempotent module-load behavior).

## Verification matrix (plan → spec contract)

| Spec contract | Verified by |
|---|---|
| 1 Exact-value masking | Task 4 acceptance test (Redactor masks resolved value) |
| 2 Allowlist confinement | Task 3 symlink/escape tests + Task 4 escape-rejected test |
| 3 Register-before-return | Task 4 no-skip post-condition (`V in snapshot()`) + positive post-resolve Redactor |
| 4 Empty-value drop | Task 1 no-op register + Task 4 empty-file test |
| 5 Refcount eviction | Task 1 release tests + Task 4 scope-plumbing test |
| 6 URL credential strip | Task 2 `redact_url_credentials` tests |
| 7 Logging-filter version cache | Task 2 `SecretRedactionFilter` rebuild test |
