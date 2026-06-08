# ADR 0070 — Fleet availability + system reuse (M1.4)

- **Status:** Proposed
- **Date:** 2026-06-08
- **Deciders:** platform maintainers
- **Builds on (does not supersede):** [ADR-0067](0067-system-shapes-catalog.md) (the shapes
  a host is measured against), [ADR-0068](0068-custom-config-pcie-modeling.md) (the device
  matcher the filters reuse), [ADR-0023](0023-discovery-allocation-admission.md) (the
  per-host capacity counters availability reports), [ADR-0026](0026-investigation-run-lifecycle.md)
  (the Run/System lifecycle the reuse path joins).
- **Spec:** [`../specs/m1.4-system-catalog-scheduling.md`](../specs/m1.4-system-catalog-scheduling.md)

## Context

With shapes (ADR-0067) and PCIe descriptors (ADR-0068) in place, an agent still has no way
to ask **what is free right now** or **what already exists that it could reuse**.
`resources.list` / `.describe` report static capability and health, not live headroom;
there is no System inventory view at all.

`runs.create(investigation_id, system_id, …)` already takes a `system_id`, and iterating
many Runs against one persistent System is the existing baseline (top-level design §Run) —
provisioning is a separate `systems.provision` step, not part of `runs.create`. So this
milestone does **not** add a new attach mechanism or a "skip provisioning" path; both
already exist. What is missing is the ability to **discover** a reusable System the agent
did not just provision (e.g. one provisioned under a different Investigation, or found in a
shared pool) and to **validate** that attaching to it is safe — exactly the inventory view
and the reuse preconditions this ADR adds.

## Decision

We will add two `viewer` reads and one reuse path.

**`resources.availability`** reports, per host: the free headroom, the free PCIe devices,
and the shapes that fit now. Headroom uses the **same occupancy predicate as admission** —
`cap − count(GRANTED / ACTIVE / RELEASING)` (ADR-0069) — so queued `requested` rows are
**excluded** from per-host headroom; availability never shows a host fuller or emptier than
the admission gate treats it. Queue depth is reported at **fleet/kind granularity**, not
per host: a queued row's `resource_id` is null until promotion (ADR-0069), so a by-kind
queued request is not attributable to any one host. Availability is **schedulability-aware**
like admission and the sweep: a `cordoned` or non-`available` host (ADR-0062) is reported
with an explicit non-schedulable flag and never counts as "fits now", so availability never
points the agent at a host every request would refuse. A shape "fits now" iff a
**schedulable** host has both the size headroom **and** a free PCIe device for the shape's
`pcie_match` (ADR-0067/0068). It accepts a `pcie` match filter and a `shape` filter so
"which hosts have a free X710?" or "which hosts fit `large`?" is one call.

**`systems.list`** reports Systems scoped to the caller's projects, filterable by
`allocation_id`, `state`, `shape` (a named shape, or a sentinel for full-custom `shape IS
NULL`), and a `pcie` match.

**Reuse.** The new capability is **discover-and-validate**, not a new attach call:
`runs.create` already names a `system_id`. ADR-0070 lets an agent attach a Run to a `ready`
System it did **not** just provision — found via `systems.list`, possibly provisioned under
a **different Investigation** or in a shared pool — and defines when that attach is safe.
The Run still builds, installs, and boots its kernel onto the reused System (ending any
prior DebugSession at reboot); no System provision runs either way (provisioning was always
separate). Reuse is **explicit** — the agent chooses the System — never an implicit
match-or-provision. The **reuse match predicate** (which ADR-0067 delegated here) is the
persisted **sizing snapshot**, not the shape name: a System is reusable for a request iff
its snapshot sizing **is ≥ the request's resolved tuple** and its PCIe claim union
**contains** the request's required devices. The "≥" (not exact) is deliberate — the System
is already allocated and billed under its own Allocation, so an over-sized reuse costs the
requester nothing extra and raises the hit rate; exact-match would needlessly miss a usable
larger System. A size-equal full-custom System is therefore reusable, and a shape later
resized under the same name does **not** falsely match an old System (consistent with
ADR-0067's snapshot identity); the shape *name* is a convenience filter on `systems.list`,
not the reuse identity.

These are `runs.create` preconditions **in general** — there is one code path
(`runs.create(system_id)`), and "reuse" is only the case where the System was not
self-provisioned, so every `runs.create` enforces them (the self-provisioned path does not
skip them); reuse is just where live-allocation and single-project bite most visibly. All
three are re-validated under the per-System / per-Allocation lock, not merely at
name-resolution:

1. **Live allocation** — the System's Allocation is non-terminal (lease live); a System on
   a terminal/expiring Allocation (the ADR-0021 orphan-reaping window) is `stale_handle`,
   never a silent attach.
2. **Single project** — the System's project (its Allocation's project) **equals** the
   Run's Investigation project; reuse crosses Investigations only *within one project*, so
   attribution/billing stays single-project. Cross-project reuse is `configuration_error`.
3. **One Run per System** — at most one **non-terminal Run** may hold a System at a time
   (the per-System lock). A System is one bootable instance; reusing one that has an
   in-flight Run would race two installs/boots into `transport_conflict`/corruption, so
   reuse of a System with a live Run is rejected (`transport_conflict`), not merely gated on
   `ready`.

## Consequences

- The availability and systems filters reuse the ADR-0068 matcher and the ADR-0067 shape
  resolver, so the "find hardware" and "find a reusable System" workflows share one code
  path and one match grammar.
- `systems.list` honors the no-leak rule — a System in an ungranted project is omitted,
  indistinguishable from absent (ADR-0023), so the cross-project view is safe at `viewer`.
- Reuse lets an agent attach to a `ready` System found via `systems.list` (including one
  from another Investigation or a shared pool) instead of provisioning a fresh System,
  cutting the per-Run cost on the persistent-System loop; a stale or mismatched target is a
  `stale_handle` / `configuration_error`, never a silent reprovision. (Attaching to a System
  the agent itself just provisioned was already possible; what is new is the safe-reuse of
  one it did not.)
- Availability is a point-in-time read, not a reservation — a host shown free can be taken
  by a concurrent grant before the agent requests it; the request path (admission /
  scheduler) remains the authority, and the agent treats availability as a hint.
- Keeping reuse explicit avoids the surprise of an agent's Run landing on a System it
  didn't expect (different kernel, different debug state); the agent that wants a fresh
  System still provisions one.

## Alternatives considered

- **Automatic reuse in `runs.create`** (transparent match-or-provision). Less agent
  ceremony, but lands a Run on a System the agent didn't pick — wrong kernel or leftover
  debug state — and hides the cost decision; rejected for explicit reuse plus
  `systems.list` visibility.
- **Availability as a reservation** (hold the slot the view reported). Removes the
  point-in-time race, but duplicates the admission/scheduler authority and invites held-
  but-unused slots; rejected — availability is a hint, the request path is the authority.
- **Fold availability into `resources.describe`.** Fewer tools, but conflates static
  capability with live headroom and has no natural place for the cross-host PCIe filter;
  rejected — availability is its own read.
