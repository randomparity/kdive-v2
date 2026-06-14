# Object-store quarantine for pre-registration writes (design)

- **Issue:** [#190](https://github.com/) (ADR-0073 follow-up)
- **Decision record:** [ADR-0075](../../adr/0075-objectstore-quarantine-pre-registration-writes.md)
- **Builds on:** [ADR-0073](../../adr/0073-forced-secret-resolution-redaction.md) (forced
  secret resolution + the load-bearing "release follows redact-and-persist" ordering),
  [ADR-0027](../../adr/0027-safety-modules-secret-backend-impl.md) /
  [ADR-0012](../../adr/0012-secret-backend.md) (the register-before-return `SecretBackend`),
  [ADR-0013](../../adr/0013-object-store-layout-retention.md) /
  [ADR-0017](../../adr/0017-object-store-client-interface.md)
  (the object-store layout and sensitivity-on-metadata),
  the cross-cutting redaction contract in [`top-level-design.md`](../../specs/top-level-design.md)
  §Cross-cutting concerns.

## Purpose

ADR-0073 (M1.5) implemented the **in-line** masking loop only: resolve a secret → emit its
value into a transcript → redact → persist → release, all while the value is registered in
the redaction registry. It named, but explicitly did **not** implement, one case: an artifact
**persisted in a separate write before secret registration completes**. At that write there is
no registered value to mask, so the bytes land **raw**. ADR-0073 routed this to a follow-up
(this issue): such a write should be **stored raw, flagged quarantined, and never served
clean**.

This design implements that quarantine path. The only shipped provider (local-libvirt)
resolves no secrets, so — exactly as ADR-0073 did for the in-line loop — the **fault-inject
provider is the test vehicle** that drives the mechanism on a synthetic high-entropy sentinel.

## The constraint that shapes the design

The redaction registry (`security/secrets/secret_registry.py`) reference-counts secret values
**per op scope** and **releases them at op end** (ADR-0027): a value resolved under a per-op
scope is evicted when that scope is released, so "a resolved secret does not linger as a global
redaction needle past the op that needed it" (ADR-0073). A consequence: a read path that masks
a quarantined artifact by consulting the **live** registry would, at any access *after* the op
released its scope, find **nothing registered** and serve the raw secret. So "redacted on
access" cannot be a lazy mask against the live registry.

The redaction must therefore happen **while the value is registered** — i.e. within the op,
before scope release. This is the same ordering ADR-0073 already calls load-bearing: release
follows redact-and-persist, never precedes it.

## Design

### Quarantined sensitivity

Add a third `Sensitivity` value, `QUARANTINED = "quarantined"`, and migration `0019` widening
the `artifacts_sensitivity_check` constraint to `IN ('sensitive', 'redacted', 'quarantined')`.
It is distinct from `sensitive`: `sensitive` marks a raw artifact whose redacted sibling was
produced **at write time** (the value was registered); `quarantined` marks a raw artifact that
arrived **before registration** and therefore carries an **unfulfilled redaction obligation**.

### Never served clean (structural)

A quarantined artifact is a real, queryable row: it gets an `artifacts` **row** with
`sensitivity = 'quarantined'` (the same column the serve gates filter on), in addition to the
`quarantined` value on the object metadata. The row is what makes the guarantee testable and
the migration load-bearing — the row `INSERT` itself fails unless migration `0019` has widened
the `artifacts_sensitivity_check` constraint, so the serve-gate test below *is* the migration's
test.

Every artifact serve/list/search gate is an **allow-list** for `sensitivity = 'redacted'`:

- `mcp/tools/catalog/artifacts_reads.py` `_GET_SQL` (`… AND sensitivity = 'redacted'`),
- `services/artifact_listing.py` `_LIST_REDACTED_SYSTEM_SQL` (same predicate).

A `quarantined` row is excluded by both **by construction** — no gate admits "anything not
redacted." A DB serve-gate test inserts a `quarantined` row and asserts `artifacts.get`,
`artifacts_list`, and `artifacts_search_text` each refuse it, while a `redacted` control row on
the same System is served — pinning the exclusion so a later gate change cannot silently begin
serving a quarantined object. (The separate fetch-time check in `_artifacts_search_text` —
`if fetched.sensitivity is not Sensitivity.REDACTED` — is defense-in-depth for a *redacted*-keyed
row whose object metadata disagrees; the quarantine path never reaches it because `_GET_SQL`
excludes the row first. It is not the gate this design relies on.)

### The fault-inject quarantine loop (test vehicle)

A new fault-inject-only module `providers/fault_inject/quarantine_console.py`, mirroring the
structure of `secret_console.py` (#183). It operates at the **object-store layer** (store-only,
no DB pool — like `secret_console`), so it proves the masking/heal mechanism as a fast unit
test; the **row-level** serve-gate exclusion and the migration are proven by the separate DB
test in §"Never served clean". It exercises the full mechanism on a synthetic sentinel under a
per-op-unique scope:

1. **Pre-registration write.** Read the sentinel **raw** (confined to the allowlisted secrets
   root, size-capped) — *not* registered — modelling a provider that already holds the
   credential. Emit it into a synthetic console transcript and `put_artifact(...,
   sensitivity=QUARANTINED)`. This is the write that lands before registration.
2. **Resolve.** Resolve the same ref through the registry-bound `SecretBackend`, which
   registers the value under the per-op scope before returning it (ADR-0027).
3. **Heal.** Re-fetch the quarantined object **from the store** (`get_artifact`), redact its
   bytes with a `Redactor` built from the now-seeded registry, and `put_artifact(...,
   sensitivity=REDACTED)` under a sibling key (the object store is write-once; this mirrors the
   existing raw + `-redacted` vmcore pattern). Both objects carry `retention_class = "console"`
   (as `secret_console` does). The quarantined raw object is **retained** for provenance; it
   stays unservable. Lifecycle/reaping of the retained raw is unchanged by this design — it is
   subject to the same retention policy as any `sensitive` raw (e.g. the raw vmcore) and is out
   of scope here.
4. **Release after persist.** Release the per-op scope only in a `finally`, after the heal
   persist — so the value is registered at the moment the heal redacts, and evicted only once
   the loop returns. A failed heal still releases the scope, so a crash cannot leak a global
   redaction needle.

### Targeted refactor

Extract the confined, size-capped secret-file read out of `FileRefBackend.resolve` into a
module-level `read_secret_file(root, ref) -> str` in `security/secrets/secrets.py`.
`FileRefBackend.resolve` becomes read-then-register; the quarantine loop calls the bare read
for its unregistered pre-write. Path confinement and the 64 KiB cap live in one place and apply
to both callers.

## Components

| unit | responsibility | depends on |
|------|----------------|------------|
| `Sensitivity.QUARANTINED` + migration `0019` | the third, distinct flag (column + metadata) | `domain/models.py`, schema |
| DB serve-gate test | insert a `quarantined` row; assert get/list/search exclude it (and serve a `redacted` control) — also the migration's load-bearing test | read gates, `artifacts` table |
| `read_secret_file` | confined, capped, unregistered read | `security/secrets/paths.py` |
| `quarantine_console.py` | object-layer store-raw → resolve → heal → release loop | `objectstore`, registry, `Redactor`, backend |

## Failure modes and edges (all under test)

- **Release-after-persist ordering** — the sentinel is in the registry snapshot at the moment
  of the heal write, and absent from it after the loop returns.
- **Release on heal failure** — when the heal `put_artifact` raises, the `finally` still
  evicts the scope; the value is gone from the registry.
- **Raw stays raw** — the quarantined object's bytes still contain the sentinel and carry
  `sensitivity == QUARANTINED`; only the healed sibling lacks the sentinel and carries
  `[REDACTED]`.
- **Mask came from the registry, not a pattern** — a control `Redactor` over an empty registry
  leaves the bare sentinel present (the transcript echoes the value bare, not as
  `password=<value>`), proving the heal masked via exact-value registration, not the key/value
  regex (same guard #183 uses).
- **Concurrent-op scope isolation** (carried from ADR-0073's implementation binding) — two ops
  resolve **distinct** high-entropy sentinels under **distinct** scopes; op B releases first;
  op A's value is still registered at op A's heal write (so op A's redact masks it) and is
  evicted only on op A's own release. Distinct values prove isolation by scope, not by
  shared-value refcounting.
- **Serve gate rejects quarantined (DB test)** — insert an `artifacts` row with
  `sensitivity = 'quarantined'` for a System plus a `redacted` control row; assert
  `artifacts.get` on the quarantined id returns the not-found-shaped config error,
  `artifacts_list` omits it, and `artifacts_search_text` on it returns the config error — while
  the `redacted` control row is served by each. The quarantined-row `INSERT` succeeding is
  itself the proof migration `0019` widened the constraint.
- **Migration rejects out-of-set** — an `INSERT` with a sensitivity value outside
  `('sensitive','redacted','quarantined')` still violates `artifacts_sensitivity_check`.

## Out of scope

- **No production caller.** local-libvirt resolves no secrets; the loop is fault-inject-only,
  absent from the default composition (same as #183's `secret_console.py`).
- **No lazy redact-on-access read tool.** Rejected in ADR-0075 — unsound against the live
  registry post-release, and a by-reference re-resolution read path has no caller yet (YAGNI).
- **No new agent-facing MCP tools** — consistent with M1.5's no-new-tools constraint.
