# ADR 0003 — Six durable objects replace the run-centric model

- **Status:** Proposed
- **Date:** 2026-06-03
- **Implements core decision:** #3 in [`../specs/top-level-design.md`](../specs/top-level-design.md)

## Context

The PoC bundles build + boot + debug into one run. Production splits the domain
into six durable objects with independent lifecycles — Resource, Allocation,
System, Investigation, Run, DebugSession — where Investigation is a cross-cutting
grouping that may span Allocations and resource kinds. See the spec's "Domain
model".

## Decision

We will model the domain as **six durable Postgres-backed objects** — Resource,
Allocation, System, Investigation, Run, DebugSession — each with an explicit
state-machine column, replacing the PoC's single run-centric object.
Resource → Allocation → System → Run nest as lifecycles (a lower layer outlives
the higher one); Investigation is a cross-cutting grouping independent of any one
Allocation; DebugSession is a sub-object of a Run bounded by one boot. A Run is
the join of exactly one System and exactly one Investigation.

## Consequences

- More tables and state machines than the run-centric model, but each lifecycle is
  independent and separately reconcilable.
- Reprovision-in-place (a new System under the same Allocation) and investigations
  that span Allocations and resource kinds become expressible without special
  cases.
- Every object needs its own transition rules, idempotency, and audit — one
  uniform pattern applied six times.
- Reconciliation operates per object (orphaned System, dead DebugSession, idle
  Investigation are distinct repairs); see [0005](0005-postgres-object-store-state.md)
  and the spec's "Reconciliation & teardown".

## Alternatives considered

- **Keep the run-centric PoC object.** Rejected by core decision #3: it cannot
  express multi-user leasing, reprovision, or investigations that span allocations.
- **Fold System into Allocation.** Rejected: a System is reprovisionable in place
  with a distinct lifecycle; merging loses the "install a kernel and reboot ≠ a new
  System" invariant.
- **Make DebugSession a field on Run rather than its own object.** Rejected: a
  session is a durable row (state, transport handle, worker heartbeat) so the
  reconciler can detect a `live` session whose transport died; that needs an
  independent lifecycle within a Run, not a single column.
- **Event-sourced log instead of state rows.** Rejected: heavier than M0 needs;
  explicit state machines are simpler to reason about and to lock.
