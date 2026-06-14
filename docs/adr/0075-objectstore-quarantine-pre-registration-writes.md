# ADR 0075 — Object-store quarantine for pre-registration writes (M1.5)

- **Status:** Proposed
- **Date:** 2026-06-08
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0073](0073-forced-secret-resolution-redaction.md)
  (the in-line masking loop and the load-bearing "release follows redact-and-persist"
  ordering), [ADR-0027](0027-safety-modules-secret-backend-impl.md) /
  [ADR-0012](0012-secret-backend.md) (the register-before-return `SecretBackend` and the per-op
  scope), [ADR-0013](0013-object-store-layout-retention.md) /
  [ADR-0017](0017-object-store-client-interface.md) (sensitivity carried as object metadata).
- **Spec:** [`../superpowers/specs/2026-06-08-objectstore-quarantine-design.md`](../archive/superpowers/specs/2026-06-08-objectstore-quarantine-design.md)
- **Issue:** #190.

## Context

ADR-0073 implemented the in-line redaction loop (resolve → emit → redact → persist → release)
and named, but left unimplemented, the **quarantine-before-redaction** case: an artifact
**persisted in a separate write before secret registration completes** has no registered value
to mask, so it lands raw. ADR-0073 §Decision says such a write *should* be stored **raw and
flagged sensitive (quarantined), redacted on access, never served clean**, and recorded that
"quarantine is design intent, not code." Issue #190 is that follow-up.

One constraint bounds every option. The redaction registry reference-counts secret values **per
op scope** and **releases them at op end** (ADR-0027), so a resolved value does not linger as a
global redaction needle past its op (ADR-0073). Therefore a read path that masks a quarantined
artifact against the **live** registry would, at any access after the op released its scope,
find nothing registered and serve the raw secret. "Redacted on access" cannot mean a lazy mask
against the live registry.

The only shipped provider (local-libvirt) resolves no secrets, so — as with the in-line loop —
the fault-inject provider is the test vehicle.

## Decision

We will add a distinct `Sensitivity.QUARANTINED` flag (migration `0019` widens the
`artifacts_sensitivity_check` constraint) and realize "redacted on access, never served clean"
as **eager heal within the op**:

1. A pre-registration write is stored **raw** and flagged **`quarantined`**.
2. "Never served clean" is **structural**: every serve/list/search gate is an allow-list for
   `sensitivity = 'redacted'`, so a `quarantined` artifact is excluded by construction.
3. After the secret registers, and **before the op releases its scope**, the op **heals** the
   quarantine: re-fetch the raw object from the store, redact it with a `Redactor` over the
   now-seeded registry, and persist a `redacted` sibling. The quarantined raw is retained for
   provenance and stays unservable.

Healing before release keeps ADR-0073's load-bearing ordering (release follows
redact-and-persist) and means the value is registered at the exact moment the heal masks it.

## Consequences

- **The quarantine seam has a live, asserted caller for the first time**, on a synthetic
  sentinel — so a redaction gap in the pre-registration-write path is a finding surfaced now,
  before M2's remote provider emits a real credential into an early boot artifact.
- **New DDL**: migration `0019` adds `quarantined` to the sensitivity constraint. A quarantined
  artifact is a real `artifacts` row carrying that value (not object metadata alone), so the
  row `INSERT` is what exercises the widened constraint, and the serve gates exclude it without
  change (they already allow-list `redacted`). `Sensitivity` gains a value; no `match` is
  exhaustive over it today, so no call site is forced to branch.
- **New obligation**: a provider that performs a pre-registration write must flag it
  `quarantined` and heal it before releasing its op scope; a raw write left un-healed is
  unservable (fail-closed), not leaked. local-libvirt is unaffected (it resolves nothing).
- **The quarantined raw is retained**, not deleted — provenance over storage thrift; the bytes
  are unservable, so retention carries no disclosure risk.
- The fault-inject quarantine loop is **absent from the default production composition** (like
  #183's `secret_console.py`); it is exercised at the worker boundary by tests.

## Alternatives considered

- **Lazy redact-on-access against the live registry.** A read path masks the raw bytes using
  whatever is registered at access time. Rejected: the registry releases the value at op end,
  so any later access finds nothing to mask and serves the raw secret — the exact leak this
  work exists to prevent. It also can't fail closed: with no registered value it cannot tell a
  raw secret from clean text.
- **Lazy redact-on-access by re-resolution.** Store the secret ref(s) in the quarantined
  object's metadata; a read path re-resolves by reference, redacts on the fly, and fails closed
  if the ref no longer resolves. Sound and faithful to a literal "redact on access", but it
  adds a new read tool, ref-in-metadata, and a read-time scope for a path with **no caller**
  (local-libvirt resolves nothing; M2 is not here). Rejected as premature surface — eager heal
  delivers the same observable guarantee (raw never served, a redacted form exists) with less.
  If M2 ever needs lazy re-resolution, a follow-up ADR adds it.
- **Flag-only, no heal.** Add `quarantined` and exclude it from serve gates, but build no
  redaction path — a quarantined artifact is simply never readable. Rejected: it delivers
  "never served clean" but not the "redacted on access" half of the ADR-0073 mandate; the value
  the artifact carries would be permanently unrecoverable.
- **Reuse `sensitive` instead of a new flag.** Store the pre-registration write as `sensitive`.
  Rejected: `sensitive` means "raw with a redacted sibling already produced"; it carries no
  marker for the **unfulfilled** redaction obligation a pre-registration write represents, so a
  healer could not tell which raw objects still need healing.
