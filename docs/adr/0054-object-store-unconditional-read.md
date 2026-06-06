# ADR 0054 — Object-store unconditional read for system-produced keys (G2, closes #126)

- **Status:** Proposed
- **Date:** 2026-06-06
- **Issue:** #126 (G2 — install fetch seam), part of #123 (live-seam demo).
- **Refines:** [ADR-0017 §3](0017-object-store-client-interface.md) (etag consistency
  via conditional GET; both miss and mismatch are `stale_handle`) and
  [ADR-0030 §5](0030-install-boot-plane.md) (the injected `fetch_kernel`/`fetch_initrd`
  seam the install plane stages with).
- **Spec:** [`../superpowers/specs/2026-06-04-install-boot-plane-design.md`](../superpowers/specs/2026-06-04-install-boot-plane-design.md) §5.2.

## Context

The install plane stages the built kernel (and an optional initrd) to a per-Run
host-local path for direct-kernel boot (ADR-0030 §5). The slow, host-bound fetch is an
injected seam — `Fetch = Callable[[str, Path], None]` — that today `raise`s
`MISSING_DEPENDENCY` (`install.py:_real_fetch`). #126 (gap G2) implements the real seam:
resolve a build-artifact key from the object store and write its bytes to the staging
path via the existing temp-then-rename contract.

The input the seam receives is `run.kernel_ref` / the build ledger's `initrd_ref` — a
bare object **key** (`{tenant}/runs/{run_id}/{name}`). The build plane records only the
key as the Run's `kernel_ref` (`runs SET kernel_ref = output.kernel_ref`, ADR-0029 §5);
the object's **etag** is recorded separately in the `artifacts` row, not in the Run's
`kernel_ref`. So at fetch time the seam holds a key but **no etag**.

ADR-0017 §3 gave the store exactly one read path: `get_artifact(key, etag)`, which issues
a conditional `GetObject(IfMatch=etag)` and maps a 412 mismatch (and a 404 miss) to
`STALE_HANDLE`. That contract was written for the **client-serving** path
(`artifacts.get`), where the caller holds an `artifacts`-row handle (`key + etag`) and a
stale-handle check is the point — it detects a row whose object was rotated or GC'd.

The pre-existing live seams that fetch by key alone worked around the missing etag by
passing the empty string: `object_store_from_env().get_artifact(ref, "")`
(`introspect_drgn.py:_real_fetch_object`, `retrieve.py:_real_fetch_object`). An empty
etag is re-quoted into `If-Match: ""`, which **never** matches a real object etag. This
was verified against MinIO: `get_artifact(key, "")` raises `STALE_HANDLE`, while an
unconditional `GetObject` (no `IfMatch`) returns the bytes. So the `""` workaround is a
latent defect — those seams (all `# pragma: no cover - live_vm`, never run end-to-end)
would always fail a real fetch with a spurious `stale_handle`.

## Decision

### 1. `get_artifact(key, etag)` accepts `etag=None` to read unconditionally

`etag` becomes `str | None` (kept **positional and required** — no default, so a caller
must explicitly state its intent and cannot silently downgrade a stale-handle check by
forgetting an argument). A non-`None` etag keeps the ADR-0017 §3 conditional GET and its
412→`stale_handle` mapping unchanged. `etag=None` omits the `IfMatch` header and performs
an unconditional `GetObject`; a 404 still maps to `STALE_HANDLE` (the recorded key no
longer resolves — a stale ref), and the absent/invalid-metadata and transport/body-read
failures still map to `INFRASTRUCTURE_FAILURE`. No new method; the metadata and
error-mapping logic is shared by both modes.

### 2. The install fetch (and the symbolization fetches) read with `etag=None`

The install staging seam fetches a key the same Run's build just produced and recorded;
there is no client-supplied handle whose freshness is in question, so the conditional GET
is semantically wrong (it can only false-negative). `_real_fetch` resolves
`object_store_from_env().get_artifact(ref, None).data` and writes it via temp-then-rename.
The two pre-existing `get_artifact(ref, "")` seams (`introspect_drgn`, `retrieve`) are
corrected to `get_artifact(ref, None)` in the same change — they are the same key-only,
no-handle read and carried the same latent defect.

### 3. The testable core is factored behind an injected store, not the env factory

`_real_fetch` stays a thin `# pragma: no cover - live_vm` wrapper that supplies
`object_store_from_env()`. The temp-then-rename + error-propagation logic moves to
`_stage_object(store, ref, dest)`, which takes the store as a parameter and is unit-tested
with an in-memory fake store — so the staging contract is exercised host-free while only
the one `object_store_from_env()` wiring line is uncovered. The `Fetch` seam shape
(`Callable[[str, Path], None]`) is unchanged, so the install plane's injected-seam unit
tests are untouched (ADR-0030 §5, #126 acceptance).

## Consequences

- The store gains one read mode (unconditional) on the existing method; the
  client-serving stale-handle contract (ADR-0017 §3) is preserved for every caller that
  passes an etag.
- A latent `stale_handle`-on-every-fetch defect in two `live_vm` seams is removed; the
  install fetch is correct on first real use.
- A genuinely rotated/GC'd staging key still surfaces as `STALE_HANDLE` (via the 404
  mapping), so the categorized-failure contract the install handler propagates is intact.
- `get_artifact`'s signature widens to `etag: str | None`; callers passing a real etag are
  source-compatible. One MinIO-backed test pins the unconditional read; one pins that a
  missing key under `etag=None` is still `STALE_HANDLE`.

## Considered & rejected

- **Add a separate `fetch_object(key)` method for the unconditional read.** Rejected: it
  duplicates the metadata-parse + body-read + error-mapping block of `get_artifact`, and
  the two paths would drift. Widening the one method's `etag` to `str | None` keeps a
  single read implementation with the conditional check as an explicit opt-in.
- **Default `etag` to `None`.** Rejected: a default would let the client-serving path
  (`artifacts.get`) silently lose its stale-handle guard if a caller forgot the argument.
  Keeping `etag` required (typed `str | None`) forces each call site to state whether it
  holds a handle.
- **Thread the etag from the `artifacts` row through the install handler into the seam.**
  Rejected: it changes the injected `Fetch` seam shape from `(ref, dest)` to carry an etag
  (the #126 acceptance pins the seam shape) and re-introduces a conditional check the
  staging path does not need — the key comes from the Run's own freshly-written
  `kernel_ref`, not a long-lived client handle.
- **`head(key)` to read the etag, then `get_artifact(key, head.etag)`.** Rejected: two
  round trips and a TOCTOU window to reconstruct a conditional check whose only effect on
  this path is to add a failure mode (the object rotating between `head` and `get`), for a
  key nothing else writes. The unconditional GET is the honest single-round-trip operation.
- **Keep the `get_artifact(ref, "")` empty-etag idiom.** Rejected: verified to raise
  `STALE_HANDLE` against MinIO — it does not read the object at all.
