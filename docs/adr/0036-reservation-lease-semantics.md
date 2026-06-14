# ADR 0036 — Reservation / lease semantics (M1)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** M1 — Allocation/accounting depth (lease/reservation)
- **Depends on:** [ADR-0007](0007-metering-budgets-admission.md) (the reserved
  estimate is `rate × lease_window`), [ADR-0021](0021-reconciler-loop-drift-repair.md)
  (the M0 lease-expiry compensation this triggers), [ADR-0023](0023-discovery-allocation-admission.md)
  (the admission path the lease window attaches to)
- **Concurrency owned by:** [ADR-0040](0040-admission-lifecycle-concurrency.md) (the
  per-Allocation lock and single-reconciliation guarantee for the `→expired` sweep;
  renew idempotency)
- **Refines:** the Allocation lifecycle in
  [`../specs/top-level-design.md`](../design/top-level-design.md) and
  [`../specs/m1-allocation-accounting.md`](../design/m1-allocation-accounting.md)

## Context

M0 grants allocations with no time bound: `allocations.lease_expiry` exists as a
nullable column but is never set, and nothing expires an idle allocation. The M0
reconciler already implements the **teardown half** of lease expiry — drain in-flight
jobs within a grace window, force-kill, mark the owning Run `failed(lease_expired)`
([ADR-0021](0021-reconciler-loop-drift-repair.md)) — but nothing **triggers** it,
because no lease window is ever set. M1 ("real reservation/lease semantics") supplies
the triggering half: a window at grant, a renewal path, and a reconciler pass that
moves an expired allocation to a new terminal state and hands its System to the
existing teardown.

This also closes a capacity/cost leak: without expiry, a granted allocation holds a
per-host slot (ADR-0023), a per-project concurrency slot, and a budget reservation
(ADR-0007) **forever** if the agent walks away.

## Decision

### 1. A lease **window** is set at grant, bounded by config

`allocations.request({selector, project, window})` carries a requested `window`
(duration). A requested window must be `> 0` (else `configuration_error` — a zero or
negative window would otherwise reserve zero/negative cost, holding a slot for free or
minting budget via a negative `reserved` delta; see ADR-0007 decision 2). Admission then
clamps it: `window = min(requested, KDIVE_LEASE_MAX)` with a default of
`KDIVE_LEASE_DEFAULT` when omitted (proposed: default 4h, max 24h —
operator-configurable). The grant sets `lease_expiry = now() + window`. The reserved
ledger estimate (ADR-0007) is `rate × window_hours`, so the window is what the
project is charged to hold the claim.

### 2. New terminal Allocation state **`expired`**, distinct from `released` and `failed`

The committed state machine reaches a terminal only via `releasing → released` (an
explicit `allocations.release`) or `failed`. M1 adds **`expired`** as a terminal
reached from `granted` or `active` when the lease window closes. Three terminals now
carry distinct meaning for audit/SLO:

- **`released`** — the agent explicitly released (`allocations.release`).
- **`expired`** — the lease window elapsed; the reconciler reclaimed it. The owning
  Run (if any) still becomes `failed(lease_expired)` via the existing compensation —
  the *allocation* is `expired`, the *Run* is `failed(lease_expired)`; they are not
  the same row and must not be conflated.
- **`failed`** — admission or an operation failed the allocation directly.

Migration `0002` widens the `allocations` state CHECK to include `expired`; `state.py`
gains the `granted → expired` and `active → expired` edges with matching guard-table
tests (additive, bisectable — the M0 pattern).

### 3. `allocations.renew` extends the window, re-charged and re-checked

`allocations.renew(allocation_id, extend, idempotency_key)` extends `lease_expiry` by
`extend` (which must be `> 0`, else `configuration_error` — the same guard as the initial
window), clamped so total remaining never exceeds `KDIVE_LEASE_MAX` from now. It is
**idempotent on `idempotency_key`** (ADR-0007's `idempotency_keys` store): a replayed
renew neither re-extends the lease nor re-charges the budget — a retry after a lost
response is safe. It runs under the per-project lock (ADR-0007) and **re-checks budget
for the added window only**,
writing an incremental `reserved` ledger delta (`rate × extend_hours`). Renewal over
budget is denied (`allocation_denied`) and does **not** extend — fail-closed, the
window stands. Renewal is `operator` (it is lifecycle, not administration). Only a
non-terminal (`granted`/`active`) allocation can renew; renewing a terminal one is
`stale_handle`.

### 4. The reconciler runs an **`→expired` sweep** that feeds the existing teardown

A new reconciler pass selects non-terminal allocations with `lease_expiry < now()`
and, per allocation:

1. transitions the allocation `→ expired` and writes the `reconciled` credit (stamping
   `active_ended_at`, ADR-0007 §3). The **single-reconciliation guarantee** — that this
   and a concurrent `allocations.release` never both credit one allocation, achieved by
   each doing its terminal transition + `reconciled` write atomically under the
   per-Allocation lock — is owned by [ADR-0040](0040-admission-lifecycle-concurrency.md)
   §4 and only referenced here;
2. hands its System (if any) to the **existing** M0 orphaned-System teardown — a
   System never outlives its Allocation (ADR-0021), and an `expired` Allocation is now
   one of the "not active" states that orphans a System;
3. the existing lease-expiry compensation drains the System's in-flight job **within
   the same M0 grace window** and force-kills only after — so transitioning the
   allocation to `expired` first does **not** bypass the drain; the owning Run becomes
   `failed(lease_expired)`.

The sweep is idempotent (a second pass sees an `expired` allocation and skips it) and
emits a structured-log line per reclaimed allocation. The grace window and force-kill
are unchanged from M0 — M1 only adds the trigger and the `expired` transition, reusing
the teardown machinery already proven by M0 exit criterion 5.

**M1 vs M1.5 scope.** M1 makes the *idle* expiry path a falsifiable gate (an allocation
past its window with **no** in-flight job, plus the grace-window drain of a cleanly
completing job). The adversarial lease-expiry-**mid-job** failures — worker death
*during* the drain, a `provision` that half-applies as the lease lapses — are
deliberately the M1.5 fault-injection target, not an M1 gate: M1 wires the trigger and
the clean path, M1.5 attacks the race. This keeps M1's exit criterion honest about what
it proves.

## Consequences

- An abandoned allocation is reclaimed automatically: its host slot, project
  concurrency slot, and budget reservation all free on expiry, closing the M0 leak.
- `expired` vs `released` vs `failed(lease_expired)` lets audit distinguish
  "reclaimed by policy" from "released by agent" from "killed mid-op" — the same
  cancel-vs-kill distinction the top-level design draws, extended to time.
- The reconciler change is purely additive: a new select-and-transition pass in front
  of the existing teardown; no change to the drain/force-kill compensation.
- Renewal re-uses the per-project lock and budget check, so a renew cannot overspend
  any more than an initial grant can.
- Migration `0002` widens one CHECK and `state.py` gains two edges — the smallest
  state-machine change that expresses lease reclamation.

## Alternatives considered

- **Reuse `failed(lease_expired)` for the allocation too (no `expired` state).**
  Rejected: it conflates the Allocation's lifecycle with the Run's. The Run is
  `failed` because its work was killed; the Allocation is `expired` because its window
  closed — different objects, different reasons, and audit needs to tell them apart.
- **Hard `release` at expiry (`granted → releasing → released`).** Rejected: marking
  a reclaimed-by-policy allocation `released` makes it indistinguishable from an
  explicit agent release, erasing the policy signal; and `releasing` models
  agent-initiated teardown-in-progress, not a timeout.
- **No renewal (re-request instead).** Rejected: a long debug session would have to
  drop and re-acquire its host slot, racing other projects for it — renewal keeps a
  live investigation's claim while still re-charging and re-checking budget.
- **Set the window from the provisioning profile, not the request.** Rejected: the
  window is a claim on a Resource for a span of time and must exist at
  `allocations.request`, before any profile/System — the same reason the cost selector
  lives on the request (ADR-0007 decision 2).
