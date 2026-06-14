# ADR 0040 — M1 admission & lifecycle concurrency: lock hierarchy, request idempotency, atomic reconciliation

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** M1 — Allocation/accounting depth (concurrency & idempotency)
- **Depends on:** [ADR-0016](0016-repository-layer-locks-idempotency.md) (the M0
  advisory-lock helper + idempotency ledger this extends), [ADR-0005](0005-postgres-object-store-state.md)
  (transaction-scoped advisory locks), [ADR-0023](0023-discovery-allocation-admission.md)
  (the M0 per-resource admission lock M1 composes with)
- **Owns the concurrency/idempotency decisions referenced by:**
  [ADR-0007](0007-metering-budgets-admission.md) §5 (budget/quota gate) and
  [ADR-0036](0036-reservation-lease-semantics.md) §4 (the `→expired` sweep)
- **Refines:** the "Admission control", "Reservation / lease", and "Reconciler" sections
  of [`../specs/m1-allocation-accounting.md`](../design/m1-allocation-accounting.md)

## Context

M1 adds a second admission invariant (per-project quota + spend budget) on top of M0's
per-host capacity cap, plus lease renewal and an `→expired` reconciler sweep. These
introduce new shared mutable state — the per-project `budgets.spent_kcu` running total
and the append-only `ledger` — written from **five** paths that previously did not
contend: `allocations.request`, `allocations.renew`, `allocations.release`,
`systems.provision`, and the reconciler sweep. M0's concurrency story
([ADR-0016](0016-repository-layer-locks-idempotency.md)) gave the repository layer one
advisory-lock helper and a `(run_id, step)` idempotency ledger; M1 needs the analogous
treatment for the **admission and allocation-lifecycle** paths.

Three correctness hazards are specific to these paths and must be pinned in one place
(rather than scattered across the cost-model and lease ADRs, where review repeatedly
re-litigated them):

1. **Deadlock** — multiple paths now take more than one advisory lock.
2. **Double-charge on retry** — `allocations.request`/`renew` are synchronous and
   commit a budget debit; a client retry after a lost response would re-charge.
3. **Double-reconciliation** — `release` and the `→expired` sweep both end an allocation
   and write its `reconciled` credit; both firing corrupts `spent_kcu`.

This ADR owns the decisions that close all three. The cost model itself
([ADR-0007](0007-metering-budgets-admission.md) §1-3) and the budget/quota *policy*
(§4,6) stay there; this ADR owns *how those operations stay correct under concurrency
and retries*.

## Decision

### 1. A global total lock order: `PROJECT < RESOURCE < ALLOCATION < SYSTEM`

M1 adds `LockScope.PROJECT` (keyed by `project`) to the M0 scopes. **Every** path that
takes more than one transaction-scoped advisory lock acquires them in the fixed total
order **`PROJECT < RESOURCE < ALLOCATION < SYSTEM`**. A single pairwise rule
(project-before-resource) is insufficient once five paths share `PROJECT`; a total order
over all scopes is what guarantees deadlock-freedom. Per-path acquisition:

| Path | Locks (in order) |
|---|---|
| `allocations.request` | PROJECT → RESOURCE |
| `allocations.renew` | PROJECT |
| `allocations.release` | PROJECT → ALLOCATION |
| `systems.provision` | PROJECT → SYSTEM |
| reconciler `→expired` sweep | PROJECT → ALLOCATION → SYSTEM |

The locks are transaction-scoped (`pg_advisory_xact_lock`, ADR-0005), so they release on
commit/rollback and a crashed worker cannot hold one. `spent_kcu` lives on the `budgets`
(project) row, so **every** write to it happens under the `PROJECT` lock — which all
five paths hold — making the running total race-free without a separate lock.

### 2. Admission is an atomic check-then-debit composing M0's host cap

`allocations.request` validates inputs (ADR-0007 §2), resolves idempotency (decision 3),
then under `PROJECT`→`RESOURCE`: checks `max_concurrent_allocations` and `(limit_kcu −
spent_kcu) ≥ estimate` (the M1 per-project invariant), checks the M0 per-host
`concurrent_allocation_cap` (ADR-0023, unchanged), and on success — **in one
transaction** — inserts the `granted` Allocation, writes the `reserved` ledger row,
increments `spent_kcu`, records the idempotency key, and writes the audit row. Any
failing check returns a typed failure with **no** durable write (ADR-0023's denial rule).
The check and the debit are inseparable: were they two transactions, two concurrent
requests could both pass the budget check before either debited and overspend — the race
the `PROJECT` lock exists to prevent. `renew` is the same shape for the added window only.

### 3. Request/renew idempotency, scoped to the principal

`allocations.request` and `allocations.renew` carry a client `idempotency_key`, stored in
`idempotency_keys` with **primary key `(principal, key)`** and resolved against the
caller's `principal`. This is the synchronous analogue of the M0 job `dedup_key`
([ADR-0018](0018-job-queue-worker-execution.md)), which only guards async tools.

- **Principal-scoped, not global.** A global key namespace would let one tenant's
  client-chosen key collide with another's and resolve to a foreign `allocation_id` — a
  cross-tenant correctness and disclosure bug. The key is unique per principal; the
  resolve matches the caller.
- **Replay → original result.** A repeated key returns the stored `allocation_id` with no
  second grant, `reserved` row, or `spent_kcu` change.
- **Concurrent duplicates** are serialized by the `(principal, key)` PK: the loser's
  insert conflicts inside its transaction, then re-reads and returns the winner's stored
  result rather than surfacing a raw conflict.
- **Denials are not cached** — the key is written only in the success transaction
  (decision 2). A request denied over budget is re-evaluated on retry, which is correct:
  the budget may have changed.
- **Retention.** The store is append-only; a reconciler GC pass deletes rows past a
  retention window (it has no other reaper).

### 4. Exactly one reconciliation per allocation (release vs. expiry)

`allocations.release` and the reconciler `→expired` sweep both end an allocation and
write its `reconciled` credit; they must not both fire on one allocation. Each takes the
per-**Allocation** lock and performs its terminal transition **and** the `reconciled`
write (plus the `spent_kcu` adjustment) in one transaction under it. Whichever reaches
the `ALLOCATION` lock first makes the allocation terminal (`released` or `expired`); the
other reads a terminal state and skips — `release` on a terminal allocation →
`stale_handle`, and the sweep selects only non-terminal allocations. A double credit is
impossible even when a lease expires at the instant the agent releases. (The sweep's
*infrastructure* teardown — draining the in-flight job within the M0 grace window — is
ADR-0036 §4; this decision is only about the single ledger reconciliation.)

## Consequences

- One ADR now owns the M1 admission/lifecycle concurrency contract, the M1 counterpart to
  M0's [ADR-0016](0016-repository-layer-locks-idempotency.md). ADR-0007 §5 and ADR-0036 §4
  reference it instead of restating it, so the lock order, idempotency, and
  single-reconciliation rules have a single source of truth with rationale — not a
  pseudocode step a review can poke a hole in.
- The total lock order is a global invariant every future path must honor; new
  multi-lock paths (M1.5+) state their acquisition order against it.
- `spent_kcu` correctness rides entirely on "all writers hold `PROJECT`," which the lock
  table makes explicit and checkable.
- Idempotency adds an `idempotency_keys` table (PK `(principal, key)`) and a reconciler GC
  pass; both additive.

## Alternatives considered

- **Leave these decisions in ADR-0007 §5 / ADR-0036 §4.** Rejected: they are a distinct
  concern (concurrency integrity, not cost policy), they cross-cut five paths, and
  scattering them is exactly why four review passes each re-found a concurrency gap. M0
  separated the same concern into ADR-0016; M1 should mirror that.
- **Per-pair lock ordering instead of a total order.** Rejected (decision 1): with five
  paths sharing `PROJECT`, only a total order over all scopes is provably deadlock-free.
- **Global idempotency key namespace.** Rejected (decision 3): cross-tenant collision.
- **Debit-at-release (no reserve-at-grant) to avoid the reconciliation race.** Rejected:
  that reintroduces the overspend race decision 2 closes (see ADR-0007 §3); the
  single-reconciliation lock is the cheaper fix.
- **A dedicated lock table / row-level `SELECT … FOR UPDATE` on `budgets` instead of an
  advisory lock.** Viable, but the project advisory lock already serializes the gate and
  composes with the M0 per-resource advisory lock under one ordering discipline; mixing
  lock mechanisms would complicate the total-order proof. Deferred unless profiling shows
  the advisory lock is a bottleneck.
