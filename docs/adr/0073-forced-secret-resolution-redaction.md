# ADR 0073 — Forced secret resolution + end-to-end redaction validation (M1.5)

- **Status:** Proposed
- **Date:** 2026-06-08
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0027](0027-safety-modules-secret-backend-impl.md) /
  [ADR-0012](0012-secret-backend.md) (the by-reference `SecretBackend` and the
  register-before-return invariant),
  [ADR-0072](0072-fault-injection-provider-seeded-engine.md) (the mock provider that does the
  resolving), [ADR-0071](0071-per-kind-provider-runtime-registry.md) (the registry that
  selects it), the cross-cutting redaction contract in
  [`../specs/top-level-design.md`](../specs/top-level-design.md) §Cross-cutting concerns.
- **Spec:** [`../specs/m1.5-fault-injection-provider.md`](../specs/m1.5-fault-injection-provider.md)

## Context

The secret-by-reference contract has two halves (top-level design §Cross-cutting concerns):

1. **Register before return** — `SecretBackend.resolve()` registers the resolved value into
   the `PROCESS_SECRET_REGISTRY` *before* returning it, so a `Redactor` built from that
   registry next will mask it. `FileRefBackend.resolve()` enforces this structurally: there
   is no return path that yields the value without first calling `registry.register(value,
   scope=...)`.
2. **Mask before persist** — every guest/transcript/console output passes through that
   `Redactor` before it lands in the object store or a response snippet, so the registered
   value is replaced by **exact-value** masking, not merely by secret-*name* patterns.

Half 1 is unit-tested against `FileRefBackend`. **Half 2 has no live caller.** The only
shipped provider, local-libvirt, resolves **no** secrets — a local QEMU domain needs no BMC
password, SSH credential, or HMC token — so no production path has ever run
`resolve() → emit the value into a captured transcript → Redactor masks it on persist →
assert the persisted/returned artifact is masked`. The contract that matters most in the
distributed model (a remote provider's console capturing a resolved BMC password) is exactly
the one M0–M1.4 never exercised. The top-level design names this precisely: M1.5 "forces
secret resolution."

## Decision

We will give the fault-injection provider a **secret reference it must resolve**, make it
**emit the resolved value into a captured transcript**, and **assert the value comes back
redacted** — exercising the full register→mask→persist loop, not just half 1.

- The fault-inject resource's `capabilities` jsonb carries a **`secret_ref`** pointing at a
  **unique, high-entropy sentinel** value (a synthetic "BMC password" / SSH key, a file under
  the allowlisted `KDIVE_SECRETS_ROOT`). High-entropy is load-bearing: masking is exact-value
  `str`-replacement, so a short or common value would collaterally mask unrelated text **and**
  let the "raw value absent" assertion pass *spuriously* (the string was absent for unrelated
  reasons). The mock's `connect` (and/or `provision`) **resolves it through the runtime's
  `SecretBackend`**, which registers the value into the scoped registry before returning it
  (ADR-0027).
- The mock then **emits the resolved value into a captured console/gdb transcript** — the
  realistic failure mode: a real provider's console echoes a credential it just used. The
  transcript flows through the **normal persistence path** (the `Redactor` built from the
  same registry), so the test asserts the persisted artifact **and** any response snippet
  **both** lack the raw sentinel **and** carry the **redaction placeholder** at the expected
  position — proving **exact-value** masking end to end (asserting only absence would be
  satisfiable by a value that was never emitted).
- **Release follows redact-and-persist, never precedes it (the load-bearing ordering).** The
  registry is refcounted and snapshot-versioned: `release(scope)` drops a value at refcount
  zero, and a `Redactor` masks only values still in the snapshot. So the op **redacts and
  persists every artifact/snippet that could contain the value first, and releases the scope
  only after** — release-before-persist would evict the value and write the artifact
  **unredacted**, the exact leak this test exists to catch. The resolution runs at the
  **worker boundary** under a **per-op unique scope identity** (e.g. the job id; register and
  release use the *same* identity, and no two in-flight ops share a scope — so a concurrent
  op's release cannot evict this op's value early). The test asserts the value is gone from
  the snapshot after release, so a resolved secret does not linger as a global redaction
  needle past the op that needed it.
- **Quarantine-before-redaction** (top-level design): redaction is a **time-agnostic** text
  scan at persist time — once the value is registered, every occurrence in the
  redacted-then-persisted transcript is masked, regardless of when each line was emitted. The
  hazard the quarantine rule addresses is therefore **not** a line emitted earlier in the
  *same* transcript (that transcript is redacted as a whole *after* resolution); it is an
  artifact **persisted in a separate write before registration completes** — that write has no
  value to mask yet, so it *should* be stored **raw and flagged sensitive** (quarantined), to
  be redacted on access, never served clean. **But quarantine is design intent, not code** —
  it is unimplemented in `src/` today. So the mandated M1.5 test is the **in-line masking
  loop** above (resolve → emit-after-resolve → masked on persist); the separate-
  pre-registration-write case is a **diagnostic probe**, not a mandated assertion: finding no
  quarantine path, it records the missing mask-before-persist coverage as an M1.5 **finding**
  routed to a follow-up. M1.5 **surfaces** this seam gap (its whole purpose, before M2); it
  does **not** expand to implement object-store quarantine.

## Consequences

- The register→mask→persist loop has a **live, asserted caller** for the first time, on a
  provider that *needs* a secret — so when M2's remote-libvirt provider resolves a real SSH
  credential, the contract it depends on is already proven, not first-run in production.
- **A redaction gap is a finding surfaced now.** If any persistence path bypasses the
  `Redactor` (a snippet built before masking, an artifact stored raw), the mock's
  emit-and-assert catches it on a synthetic secret — before a real credential leaks.
- **New obligation: the worker must thread a per-op-unique registry scope** to the
  `SecretBackend`, redact-and-persist all output under it, and release it **only after** that
  persist — not at a generic "op end." local-libvirt is unaffected (it resolves nothing), but
  the seam (resolver carries a scope; release is ordered after persist) is now exercised,
  de-risking M2. This ordering is a carried invariant in the spec.
- **No new DDL** — `secret_ref` is a `capabilities` jsonb key (ADR-0072); the secret file
  lives under the existing allowlisted secrets root (ADR-0027), so the test fixture writes a
  file, it is not a schema or API surface.
- The synthetic secret is **never a real credential** — it is fixture data under the test
  secrets root, so emitting it into a transcript to prove masking carries no disclosure risk.

## Alternatives considered

- **Assert only that `resolve()` registers** (half 1), skip the emit-and-mask. Cheaper, but
  it re-tests what `FileRefBackend`'s unit tests already cover and leaves the half that has
  **no** live caller — mask-before-persist — still unproven. Rejected: the unexercised half
  is the entire reason the milestone says "forces secret resolution."
- **Force resolution but do not emit the value** (resolve, discard). Proves the resolver is
  reachable but never drives a value *through* the `Redactor`, so a persistence path that
  bypasses masking would still pass. Rejected: the failure mode M1.5 must catch is a
  resolved value reaching the store unmasked, which only an emit-and-readback assertion
  detects.
- **Exercise redaction against a hand-crafted registry in a unit test** (no provider). Tests
  the `Redactor` in isolation but not the **worker-boundary wiring** — scope creation,
  backend threading, persistence-path coverage — which is exactly where a real integration
  bug hides. Rejected: M1.5 exists to prove the seam under the real spine, not the redactor
  unit.
