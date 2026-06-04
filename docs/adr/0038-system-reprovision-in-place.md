# ADR 0038 — System reprovision-in-place (M1)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** M1 — Allocation/accounting depth (reprovision-in-place)
- **Depends on:** [ADR-0025](0025-provisioning-plane-libvirt.md) (the M0 provisioning
  plane this extends), [ADR-0024](0024-provisioning-profile-model-shape.md) (the
  profile that defines a System), [ADR-0003](0003-six-durable-objects.md) (System
  identity and the Allocation→System lifetime rule)
- **Refines:** the System lifecycle in the M0 and M1 specs

## Context

M0 provisions a System once and tears it down (M0 spec non-goal: "No System
reprovision-in-place … the `reprovisioning` transition is M1"). The `systems` state
CHECK stops at `defined, provisioning, ready, crashed, torn_down, failed` — there is
no `reprovisioning` state and no `systems.reprovision` tool. The top-level design,
however, calls for one Allocation to **host sequential Systems (reprovision in
place)** and is explicit that **installing a new kernel and rebooting does *not* make
a new System — only an OS reprovision does**. M1 supplies that transition.

This is a provisioning-plane deferral, not accounting work, swept into M1 to retire
the M0 provisioning debt while the plane is fresh.

## Decision

### 1. New `reprovisioning` state; reprovision is a re-entrant `ready → ready` cycle

Migration `0002` widens the `systems` state CHECK to include `reprovisioning`, and
`state.py` gains the edges `ready → reprovisioning` and `reprovisioning → ready`
(plus `reprovisioning → failed` for a failed reprovision). `systems.reprovision(
system_id, provisioning_profile)` drives `ready → reprovisioning → ready`, applying a
new (or the same) provisioning profile to the **same** System row under the **same**
Allocation. This is the in-place cycle: the `systems.id` is stable across it.

### 2. Reprovision **mutates** the System's identity fields; it is not a new row

System identity is `(allocation, provisioning profile, resulting OS/target
fingerprint)` (ADR-0003). A reprovision changes the profile and the fingerprint, so it
**updates** `provisioning_profile` and `target_fingerprint` on the existing row (and
re-renders/re-defines the libvirt domain, re-tagged with the same `system_id`). It is
the OS-reprovision that the design says *does* redefine the System — but as the same
durable object, because it stays under the same Allocation. A genuinely different
Allocation would be a different System; reprovision-in-place is precisely the
same-Allocation case.

### 3. Reprovision runs as a job, gated, idempotent by dedup_key

`systems.reprovision` enqueues a `reprovision` job (long-running, like `provision`)
with `dedup_key = (system_id, "reprovision", profile_digest)` — re-issuing the same
reprovision returns the existing job; a *different* target profile is a distinct key,
a distinct reprovision. The provider op declares its contract: `idempotent` (keyed by
the profile digest), `destructive` (it destroys the current OS install), cleanup
`best-effort` (a reprovision interrupted mid-apply leaves the System `failed`, not a
half-defined `ready`). Because it is destructive (it wipes the running System), it
passes the destructive-op gate ([ADR-0037](0037-rbac-hardening-role-separation.md)) —
**but** reprovision is *lifecycle*, owned by `operator`: the gate's role factor for
reprovision is `operator`, while its capability-scope and profile-opt-in factors
still apply. (Force-crash/power/teardown remain `admin`.) This is the one place M1
uses a sub-`admin` destructive role, and it is justified: reprovisioning your own
granted System is iterating, not administering.

### 4. Runs do not survive a reprovision

A reprovision changes the OS/kernel target, so any Run bound to the System's prior
boot is invalid against the new install. Reprovision requires the System to have **no
non-terminal Run** (else `transport_conflict`/`stale_handle` — you cannot reprovision
under a live Run); Runs created after the reprovision target the new install. The
binding invariant `run.system → allocation` is unchanged (the Allocation is the same);
what changes is the boot/fingerprint the next Run builds against.

## Consequences

- One Allocation can now host a sequence of OS installs without re-acquiring the host
  slot or re-charging the allocation — the iteration-is-cheap property the design
  wants, extended from "many Runs per System" to "many Systems per Allocation".
- The `systems.id` is stable across reprovisions, so the Investigation narrative and
  cost rollup (which key on the objects the Runs touched) stay coherent.
- The state-machine change is two states-worth of edges and one widened CHECK —
  additive, bisectable.
- Reprovision being `operator`-gated (not `admin`) keeps iteration in the lifecycle
  role while still passing the capability-scope and opt-in factors of the destructive
  gate, so it cannot be invoked without the granted scope.

## Alternatives considered

- **A reprovision creates a new System row.** Rejected by ADR-0003: a new row implies
  a new identity, but the Allocation is unchanged and "a System never outlives its
  Allocation" runs the other direction — sequential Systems share one Allocation. A
  new row would also orphan the prior System under a live Allocation, confusing the
  reconciler's orphaned-System rule.
- **Reprovision as `admin` (like the other destructive ops).** Rejected: reprovision
  is an agent iterating on its own granted System, not project administration; gating
  it `admin` would force every iterating agent to hold admin, recreating the M0
  collapse ADR-0037 is removing.
- **Allow reprovision under a live Run (cancel the Run implicitly).** Rejected:
  implicit cancellation hides a destructive side effect; require the caller to reach a
  terminal Run first, so the reprovision's blast radius is explicit.
