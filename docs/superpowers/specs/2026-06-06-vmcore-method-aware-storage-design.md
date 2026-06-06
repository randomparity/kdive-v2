# Method-aware vmcore storage (first-method-wins per System) — design

- **Date:** 2026-06-06
- **Issue:** [#118](https://github.com/randomparity/kdive/issues/118)
- **ADR:** [0050](../../adr/0050-vmcore-method-aware-storage.md) (the first-method-wins decision,
  key encoding, and rejected alternatives — the convergence anchor for this spec)
- **Touches:** `src/kdive/providers/local_libvirt/retrieve.py` (producer),
  `src/kdive/mcp/tools/vmcore.py` (idempotency guard + `postmortem.*` reader),
  `src/kdive/mcp/tools/introspect.py` (reader).

## Problem

`vmcore.fetch` admits one capture job per method (dedup key `…:capture_vmcore:{method}`), but the
raw core is stored under a fixed, method-agnostic object name `vmcore` and the re-capture guard
`_existing_raw_key` matches `LIKE '%/vmcore'`. After `kdump` joins `LOCAL_LIBVIRT_SUPPORTED` (#115),
a `kdump` fetch following a `host_dump` capture is admitted, runs, finds the existing `vmcore` row,
and silently returns the **host_dump** core — the agent gets the wrong method with no error.

Unreachable in M0 (`kdump` rejected at admission); this hardens the storage layer before #115.

## Approach (per ADR-0050)

One raw core per System, first method wins. Encode the method in the object key; make the
idempotency guard method-aware in the capture handler (the per-System-locked correctness boundary);
keep the readers single-core with no new tool argument.

### 1. Producer — `LocalLibvirtRetrieve.capture` (`retrieve.py`)

`capture(system_id, method)` already receives the method. Name the stored objects after it:

- raw: `vmcore-{method.value}`  (was `vmcore`)
- redacted: `vmcore-{method.value}-redacted`  (was `vmcore-redacted`)

### 2. Raw-core resolution — the `LIKE` pattern (3 readers)

The raw core is now the single key matching `%/vmcore-%` that is **not** the `-redacted`
derivative. The shared SQL gains a second predicate:

```
SELECT object_key FROM artifacts
WHERE owner_kind = 'systems' AND owner_id = %s
  AND object_key LIKE %s            -- '%/vmcore-%'
  AND object_key NOT LIKE %s        -- '%-redacted'  (bound as a value, not inlined: psycopg %% pitfall)
```

Applied at all three sites: `_existing_raw_key` (idempotency), `_resolve_postmortem`
(`postmortem.*`), and `introspect.py::_resolve` (`introspect.from_vmcore`). First-method-wins
guarantees ≤1 raw row, so each reader resolves an unambiguous core with no method argument.

### 3. Method-aware idempotency — the capture handler (`vmcore.py`)

A small helper parses the method from a raw key. It **fails fast** on a key that carries no
`/vmcore-{method}` segment: the producer only ever writes method-suffixed keys, so a bare `vmcore`
key is a real inconsistency, not a silent "different method" (which would wedge the System's
capture path with a garbage `existing_method`). Bare `vmcore` keys are unsupported post-rename.

```
def _captured_method(object_key: str) -> str:   # '.../vmcore-host_dump' -> 'host_dump'
    head, sep, method = object_key.rpartition("/vmcore-")
    if not sep or not method:
        raise CategorizedError("malformed vmcore object key", INFRASTRUCTURE_FAILURE, ...)
    return method
```

`_precheck_system` and `_finalize_capture` take the job's `method` and, on finding an existing raw
core, branch:

- existing method **==** requested → return the existing key (idempotent re-dispatch; unchanged).
- existing method **!=** requested → raise `CategorizedError(CONFIGURATION_ERROR)` with
  `details={system_id, existing_method, requested_method}`.

The reject lives in **both** places, deliberately:

- **`_precheck_system`** (before the slow `capture()` seam) rejects the common case **orphan-free**
  — no bytes are written to the object store because `capture()` is never called.
- **`_finalize_capture`** (after `capture()`, under the lock) is the **race backstop**: post-#115,
  two different-method jobs can both pass precheck (no core yet), both run `capture()`, and the
  loser's finalize re-check then rejects. The loser's already-written object is orphaned — see
  "Object-store orphan" below. This is the same shape as the existing same-method post-capture
  race; the per-System advisory lock still guarantees exactly one winner.

The agent-facing signal is the job's recorded `error_category` (`configuration_error`) — the same
mechanism every async handler failure uses. The `details` (which method) are carried on the
`CategorizedError` for logs/operators only; `queue.fail` persists the **category**, not the
details (see ADR-0050 Decision 4). M0 satisfies the issue's "fails" branch with this typed
failure; the richer synchronous "explains" arrives with the #115 admission fast-path.

### Object-store orphan (accepted)

The race backstop above leaves the loser's captured object unreferenced (its row is never
inserted). No inline cleanup is added: the object carries `retention_class="vmcore"` like any core
and is reaped by the existing retention/reconciler sweep, and the case is only reachable post-#115
under genuine concurrency. This matches the pre-existing same-method race; this change does not
introduce a new *kind* of leak, only a second trigger for the same one.

### 4. `vmcore.list` redacted filter (`vmcore.py`)

Redacted keys are now `…/vmcore-{method}-redacted`. The list filter changes from
`endswith("/vmcore-redacted")` to: `"/vmcore-" in key and key.endswith("-redacted")`.

## Test plan (TDD, handler driven directly — the prescribed boundary)

- **different-method reject (the fix):** seed a System, insert a `vmcore-host_dump` raw row,
  enqueue a `kdump` job, call `capture_handler` → raises `CONFIGURATION_ERROR`, `retriever.capture`
  not called, artifact count unchanged. (Fails today: returns the host_dump key.)
- **same-method idempotent:** existing `vmcore-host_dump` row + `host_dump` job → returns the
  existing key, no re-capture, no second row. (Updated from the current `%/vmcore` idempotency test.)
- **fresh capture:** no existing row → stores `vmcore-{method}` + `vmcore-{method}-redacted`,
  returns the raw key.
- **reader continuity:** `postmortem.crash` and `introspect.from_vmcore` resolve the
  method-suffixed raw core (fixtures updated `vmcore` → `vmcore-host_dump`).
- **redaction guard (must stay non-vacuous):** the existing guard asserts
  `not key.endswith("/vmcore")` — after the rename **no** key ends in `/vmcore`, so that assertion
  becomes a tautology and silently stops protecting. Replace it with a check against the *new* raw
  shape: a raw core is any key with a `/vmcore-` segment that does **not** end in `-redacted`;
  assert no such key appears in any read response.
- **malformed key:** `_captured_method` raises on a bare `vmcore` key (no `/vmcore-` segment).

## Out of scope

- The synchronous admission-layer reject (unreachable until #115; ADR-0050 Decision 4).
- Any vmcore delete/replace tool (no current consumer; ADR-0050 Consequences).
- Retaining more than one core per System.
