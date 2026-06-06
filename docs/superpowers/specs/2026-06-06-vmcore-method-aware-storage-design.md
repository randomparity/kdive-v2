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

A small helper parses the method from a raw key:

```
def _captured_method(object_key: str) -> str:   # '.../vmcore-host_dump' -> 'host_dump'
    return object_key.rsplit("/vmcore-", 1)[-1]
```

`_precheck_system` and `_finalize_capture` take the job's `method` and, on finding an existing raw
core, branch:

- existing method **==** requested → return the existing key (idempotent re-dispatch; unchanged).
- existing method **!=** requested → raise `CategorizedError(CONFIGURATION_ERROR)` with
  `details={system_id, existing_method, requested_method}`. The worker dead-letters the job; the
  agent learns from the job's failure state (no silent substitution). Enforced under the existing
  per-System advisory lock, so two racing different-method jobs resolve to one winner + one reject.

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
- **redaction guard:** the surface-wide guard still finds no raw key in any read response, updated
  for the new raw-key shape.

## Out of scope

- The synchronous admission-layer reject (unreachable until #115; ADR-0050 Decision 4).
- Any vmcore delete/replace tool (no current consumer; ADR-0050 Consequences).
- Retaining more than one core per System.
