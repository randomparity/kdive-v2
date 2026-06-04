# Safety modules & file-ref secret backend — Design

**Issue:** #25 (M0) · **Depends on:** #3 (greenfield scaffold — closed) ·
**Decisions:** [ADR-0027](../../adr/0027-safety-modules-secret-backend-impl.md) (the
implementation shapes this spec realizes),
[ADR-0012](../../adr/0012-secret-backend.md) (secret-backend policy) ·
**Parent spec:** [`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md)
("Cross-cutting concerns → Secrets by reference", "Redaction")

## Goal

The redaction, path-safety, and by-reference secret-resolution primitives that the
later debug/retrieve planes depend on for transcript/vmcore redaction. Ported from
the PoC `kdive.safety.{redaction,secret_registry,paths}` into the existing
`kdive.security` package, plus a new `secrets.py`:

- `src/kdive/security/secret_registry.py` — `SecretRegistry` +
  `PROCESS_SECRET_REGISTRY`: the process-global, thread-safe, refcounted store of
  known secret values that seeds redaction.
- `src/kdive/security/redaction.py` — `Redactor`, `redact_url_credentials`,
  `SecretRedactionFilter`, `REDACTION`: exact-value + pattern redaction for the
  return/persistence path and the logging boundary.
- `src/kdive/security/paths.py` — `PathSafetyError` + `confine_to_root`: the scoped
  path-safety primitive the file-ref backend uses to keep a reference inside an
  allowlisted root.
- `src/kdive/security/secrets.py` — `SecretBackend` Protocol + `FileRefBackend`: the
  by-reference resolver that confines to an allowlisted root, registers the value
  into the redaction registry **before** returning it, and never reads a value for a
  ref that escapes the root.

Out of scope for #25: any plane wiring (debug/retrieve/transcript), the object-store
quarantine of caller-buffered output (owned by the consuming plane per ADR-0012), and
the PoC `paths.py` validators that depend on `SecretReference`/`read_elf_build_id`/
run-dir confinement (they return with the planes that own them — ADR-0027 §4).

## Non-goals

- No manager backend (Vault / cloud secret manager). The `SecretBackend` Protocol
  exists so one drops in later with no call-site change (ADR-0012).
- No env/keyring/external reference kinds. M0 ships file-ref only.
- No change to `rbac.py`/`audit.py`/`gate.py` — additive package members only.

## Surface

### `secret_registry.py`

`SecretRegistry`:

- `register(value: str | None, *, scope: object | None) -> None` — refcount `value`
  under `scope`; `scope=None` registers under the global key (never evicted).
  Empty/`None` is a no-op that does **not** bump `version` (it changes nothing the
  redactor would see). A non-empty register bumps `version`.
- `release(scope: object | None) -> None` — drop one refcount for each value the
  scope holds; `scope=None` is a no-op. Bumps `version` only if the scope held
  values.
- `snapshot() -> frozenset[str]` — the currently-registered values.
- `version() -> int` — monotonic; lets a cache detect change.

`PROCESS_SECRET_REGISTRY = SecretRegistry()` — the single process-global instance.

### `redaction.py`

- `REDACTION = "[REDACTED]"`.
- `redact_url_credentials(url: str) -> str` — strip `user:pass@` userinfo; on parse
  failure return `REDACTION` (never echo, never raise).
- `Redactor(secret_values=None)` — seeds from `PROCESS_SECRET_REGISTRY.snapshot()`
  plus any explicit values. `redact_text` / `redact_mapping` / `redact_value`.
- `SecretRedactionFilter(registry)` — `logging.Filter` that masks rendered message +
  exception/stack text against the registry; rebuilds its cached `Redactor` only when
  `registry.version()` changes.

### `paths.py`

- `PathSafetyError(ValueError)`.
- `confine_to_root(path: Path, *, allowed_root: Path) -> Path` — reject shell
  metacharacters/control chars, resolve `path` (following symlinks in existing
  components, normalizing a not-yet-created tail lexically), and require the result
  under `allowed_root.resolve()`; raise `PathSafetyError` otherwise. Returns the
  resolved path; existence is **not** asserted here — the secret backend layers the
  existing-file check on top (see `secrets.py`), so a final-component symlink whose
  *target exists outside the root* is caught by the containment check while a
  not-yet-existing tail under the root is admitted lexically. The check is
  point-in-time: a TOCTOU window exists between confine and any later use of the
  path. For M0 this is bounded by worker-host filesystem trust (ADR-0012); a caller
  that acts much later must re-confine.

### `secrets.py`

- `SecretBackend(Protocol)` — `resolve(self, ref: str) -> str`.
- `FileRefBackend(root: Path, registry: SecretRegistry | None = None, *,
  scope: object | None = None)`:
  - `registry=None` resolves to `PROCESS_SECRET_REGISTRY` inside `__init__` (a
    sentinel, not a mutable default argument bound at definition time). A
    default-constructed backend therefore shares the process-global redaction state
    — that is the production intent (resolved secrets must be masked process-wide);
    tests opt out of the shared global by passing a local `SecretRegistry()`.
  - `resolve(ref)` → `confine_to_root(Path(ref), allowed_root=root)`; require the
    confined path to be an existing file (else `PathSafetyError`,
    `secret file does not exist`); read text (UTF-8), strip a single trailing line
    terminator (`\r\n` if present, else one `\n`) and nothing else — a trailing
    space/tab inside the value is significant and preserved;
    `registry.register(value, scope=scope)`; return `value`. Registration is the
    last statement before `return`, and there is **no** return path that yields the
    value without first registering it — neither the empty-file path (which returns
    `""`, a no-op register) nor any error path (which raises instead of returning).
  - A ref escaping `root` raises `PathSafetyError` from `confine_to_root` **before**
    any read.

## Behavioral contracts (falsifiable)

1. **Exact-value masking.** After `FileRefBackend.resolve` registers a value `V`, a
   fresh `Redactor().redact_text(text containing V)` replaces every occurrence of `V`
   with `REDACTION`. *Falsified if* `V` survives in the output.
2. **Allowlist confinement.** `resolve("../escape")` (and an absolute path outside
   `root`, and a symlink under `root` whose target is an existing file outside
   `root`) raises `PathSafetyError` and reads no file. *Falsified if* it returns a
   value or reads the target.
3. **Register-before-return ordering (no-skip).** The security property is that
   `resolve` has **no** return path that yields `V` without first registering it, so
   any consumer building a `Redactor` after `resolve` returns will mask `V`. Tested
   two ways: (a) a registry double whose `register` *is what makes the value
   visible* — its `register` appends to a list and the test asserts that immediately
   after `resolve` returns, that list contains `V` (i.e. registration happened on the
   path taken, not just textually present); (b) a positive end-to-end check that a
   fresh `Redactor(registry.snapshot())` built right after `resolve` masks `V` in
   sample text. *Falsified if* `resolve` can return `V` with the double's list empty,
   or if the post-resolve `Redactor` fails to mask `V`.
4. **Empty-value drop.** Resolving a ref to an empty (or single-`\n`) file returns
   `""` and the registry snapshot does not gain an empty string. *Falsified if* an
   empty string enters the registry (which would force-mask every string).
5. **Refcount eviction.** A value registered under a bounded `scope` then `release`d
   leaves the snapshot; a `scope=None` value survives `release(None)`. *Falsified if*
   eviction drops a still-referenced value or evicts a global value.
6. **URL credential strip.** `redact_url_credentials("https://u:p@h/x")` →
   `"https://h/x"`; a malformed URL → `REDACTION`; a clean path containing `:...@`
   with a real netloc is not mangled. *Falsified if* credentials survive or a clean
   URL is corrupted.
7. **Logging-filter version cache.** `SecretRedactionFilter` rebuilds its `Redactor`
   exactly when `registry.version()` changes; a record logged after a new
   registration is masked. *Falsified if* a newly registered value leaks through a
   stale cached `Redactor`.

## Test plan

`tests/security/test_secret_registry.py`, `test_redaction.py`, `test_paths.py`,
`test_secrets.py` — one per module, mirroring package structure. Handlers/units are
tested directly (no MCP). Filesystem is a real `tmp_path` (the boundary under test is
path-safety, so a real fs is the correct fixture); a register-order stub is used for
contract 3. Edge/error paths: ref escaping root (relative `..`, absolute outside,
symlink escape), nonexistent file, empty file, value containing regex metacharacters,
malformed URL, refcount release of global vs bounded scope.

## Risks & mitigations

- **Shared `PROCESS_SECRET_REGISTRY` global leaks across tests.** Tests that register
  process-globally pollute later tests' redaction. Mitigation: tests construct a
  *local* `SecretRegistry()` and pass it to `FileRefBackend(registry=...)`; only the
  explicit global-survival test touches `PROCESS_SECRET_REGISTRY`, and it cleans up.
- **Conflict with siblings on `kdive.security`.** Only additive new modules; no edit
  to `rbac`/`audit`/`gate`/`__init__.py` beyond what the port needs. Low conflict risk.
