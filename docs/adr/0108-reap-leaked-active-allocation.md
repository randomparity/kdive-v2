# ADR 0108 — Reap leaked `active` allocations

- **Status:** Proposed
- **Date:** 2026-06-13
- **Depends on:** [ADR-0036](0036-reservation-lease-semantics.md) (the `→expired` lease sweep
  this one is the symmetric complement of), [ADR-0007](0007-metering-budgets-admission.md) (the
  reserve-at-grant / reconcile-at-release ledger the reclaim reuses), [ADR-0069](0069-reservation-pending-queue-scheduler.md)
  (the `OCCUPYING` host-cap predicate a leaked `active` allocation wedges, and the promotion
  sweep that fills the freed slot).
- **Spec:** [`../superpowers/specs/2026-06-13-reap-leaked-active-allocation-design.md`](../superpowers/specs/2026-06-13-reap-leaked-active-allocation-design.md)
- **Issue:** [#371](https://github.com/randomparity/kdive/issues/371).

## Context

The System teardown job drives a System to `torn_down` but never releases its allocation
(`jobs/handlers/systems.py:teardown_handler`). A failed or interrupted lifecycle run therefore
leaves an allocation in `active` whose single System is terminal (`torn_down` / `failed`) — or,
defensively, has no System row at all. `active` is in admission's `OCCUPYING` set
(`services/allocation/admission.py`), so the leaked allocation permanently holds its host-cap
slot; on a `cap=1` remote host this blocks every future allocation.

The existing `→expired` sweep (ADR-0036) reclaims only allocations whose **lease window has
elapsed** (`lease_expiry < now()`). A leaked `active` allocation may still be inside its lease,
so `ops.reconcile_now` reports `expired_allocations:0` and the slot stays wedged. The only
recovery today is the break-glass `ops.force_release` (`platform_admin`) — observed used 3× in
one MCP coverage session (F4). The reconciler already has the **inverse** repair,
`repair_orphaned_systems` (terminal allocation + live System → teardown); the missing half is
a live (`active`) allocation + terminal/absent System → release.

## Decision

Add a reconciler repair `reap_orphaned_active_allocations` that releases an allocation iff:

1. it is `active`; **and**
2. it has no **live** System — `NOT EXISTS` a `systems` row for it in the canonical
   non-terminal-System set (`_NON_TERMINAL_SYSTEM` from `services/systems/admission.py`:
   `defined`, `provisioning`, `ready`, `reprovisioning`, `crashed`), imported rather than
   re-spelled so it cannot drift; **and**
3. its `updated_at` is older than a `GRACE` window (`DEFAULT_ORPHANED_ACTIVE_GRACE =
   2 min`) — a mid-provision-race guard so the reaper never touches an allocation whose System
   is being (re)created right now.

This predicate is safe because there is exactly **one System per Allocation**, the System and
the allocation's `→active` stamp are committed in one transaction, and a terminal System is a
one-way door — so a live lifecycle can never be running on an allocation whose System is
already terminal. A `crashed` System is **live**, not terminal, so an allocation held for an
in-progress crash investigation (the central kdive workflow) is preserved.

The reclaim runs per candidate under `advisory_xact_lock(PROJECT) → advisory_xact_lock(ALLOCATION)`
(the lock order the expiry sweep and release service share, so reaper / release / expiry
serialize). Under the lock it re-reads the allocation, re-checks the predicate, then releases
via the **shared** `release_with_backstops` mechanic (`services/allocation/release.py`) — the
same path `ops.force_release` uses for this leak today — passing the guard-exempt
`record_system` audit writer under `system:reconciler`. A non-`released` outcome (lost race,
terminal) is skipped and retried next pass; the repair is per-candidate isolated so one failure
never starves the rest.

The repair is wired into `reconcile_once` **after** the `→expired` sweep and **before** the
promotion sweep, so a slot it frees is filled the same pass. A new `ReconcileReport` field
`reaped_active_allocations` and an `ops.reconcile_now` response key of the same name report the
count.

## Alternatives considered

- **Flip `active → expired` like the lease sweep.** Rejected: `expired` is specifically the
  lease-elapsed outcome (ADR-0036); an orphan reclaim is "work done/abandoned, return the
  slot", which is `released`. Reusing `release_with_backstops` also avoids a second copy of the
  active-ended-stamp + reconcile + audit logic.
- **Release the allocation from the teardown job handler.** Rejected: teardown can be driven by
  paths that legitimately keep the allocation (reprovision tears nothing down; break-glass
  teardown is System-scoped) and the job can fail/retry independently of allocation state. The
  reconciler is the established convergence point for cross-object drift (ADR-0021), and a
  reaper recovers leaks from **any** cause (crashed worker, lost job), not just clean teardown.
- **No grace window (predicate alone).** Rejected as defense-in-depth: the one-System / one-way
  terminal invariant already makes the predicate safe, but the 2-min grace cheaply removes any
  residual read-then-act window against an in-flight reprovision, at the cost of a ≤2-min delay
  before a genuinely leaked slot is returned — acceptable for a background reaper.

## Consequences

- A leaked `active` allocation is reclaimed automatically within one reconcile interval past
  the grace window, freeing the host-cap slot without `force_release`.
- `ops.force_release` remains the immediate-recovery break-glass tool; the reaper makes routine
  leaks self-heal.
- The reaper shares the release mechanic and lock order with the release service and expiry
  sweep, so no new transition or ledger semantics are introduced and the three paths cannot
  double-reconcile an allocation.
