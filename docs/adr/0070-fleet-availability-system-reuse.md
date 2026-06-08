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
there is no System inventory view at all. The agent's real loop is many Runs against one
persistent System (top-level design §Run), so reprovisioning a fresh System for every Run
when a matching `ready` one already exists is wasted work on the scarce host.

## Decision

We will add two `viewer` reads and one reuse path. **`resources.availability`** reports,
per host: the free headroom (`cap − non-terminal allocations`), the free PCIe devices, and
the shapes that fit now; it accepts a `pcie` match filter and a `shape` filter so "which
hosts have a free X710?" or "which hosts fit `large`?" is one call. **`systems.list`**
reports Systems scoped to the caller's projects, filterable by `allocation_id`, `state`,
`shape`, and a `pcie` match. The **reuse path** lets `runs.create` name an existing
`ready` System whose shape matches the Run's intent, skipping provisioning; reuse is
**explicit** — the agent chooses the System — never an implicit match-or-provision.

## Consequences

- The availability and systems filters reuse the ADR-0068 matcher and the ADR-0067 shape
  resolver, so the "find hardware" and "find a reusable System" workflows share one code
  path and one match grammar.
- `systems.list` honors the no-leak rule — a System in an ungranted project is omitted,
  indistinguishable from absent (ADR-0023), so the cross-project view is safe at `viewer`.
- Reuse skips the provisioning job entirely when a `ready` System matches, cutting the
  per-Run cost on the persistent-System loop; a stale or mismatched target is a
  `stale_handle` / `configuration_error`, never a silent reprovision.
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
