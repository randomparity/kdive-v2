# ADR 0009 — Capability-based provider dispatch

- **Status:** Proposed
- **Date:** 2026-06-03
- **Implements core decision:** #9 in [`../specs/top-level-design.md`](../specs/top-level-design.md)

## Context

Providers are the extension seam: each implements one or more narrow plane
interfaces for a resource `kind` and advertises only the capabilities it actually
implements. The core dispatches by matching an operation against advertised
capabilities and never hardcodes provider names — the design's bet that a new
provider needs zero core changes (a falsifiable hypothesis tested at M2). Each
plane op also declares its cancel/abandon cleanup guarantee. See the spec's
"Provider / capability model" and "Roadmap".

## Decision

Providers register **capabilities** keyed by `(plane, operation, resource_kind)`,
and the core **dispatches by matching a requested operation to an advertised
capability — never by provider name**. **When more than one provider matches a
requested `(plane, operation, resource_kind)`, selection is deterministic** — an
explicit provider/resource pin on the Allocation wins; absent a pin, the core
orders candidates by a stated policy (health, then `cost_class`, then a stable
tiebreak), never by order of registration. A provider implements only the planes
it supports; capabilities advertise that partial surface. Each plane operation
declares contract flags — idempotent, destructive, cancelable, long-running (job)
vs synchronous, and its **cancel/abandon cleanup guarantee**
(clean-rollback / best-effort / orphan-flagged) — which drive job routing, the
destructive-op gate, and the reconciler.

## Consequences

- Adding a provider (M2 remote, M5 PowerVM) is a new package that registers
  capabilities — the bet is zero `core/*` change, a falsifiable hypothesis measured
  by diff scope at M2.
- The core needs a capability registry and a matcher; dispatch gains one
  indirection.
- Partial providers are first-class: a provider lacking, e.g., force-crash simply
  does not advertise it, and the gate denies the op rather than failing late.
- A provider that advertises a capability it cannot actually honor fails with a
  typed `not_implemented` / `infrastructure_failure` at dispatch, not silently;
  where a provider exposes a `reconcile` / `list-owned` surface, advertised claims
  are checked against it.
- Contract flags are mandatory metadata on every op — more up-front declaration,
  but the reconciler and cancel paths become well-defined rather than ad hoc.

## Alternatives considered

- **A hardcoded provider switch in the core.** Rejected by core decision #9: every
  new provider edits the core, defeating the extension seam.
- **One mega-interface every provider must fully implement.** Rejected: not every
  provider does every plane (no BMC on a local VM); capabilities must express
  partial support.
- **Per-provider bespoke APIs with an adapter each.** Rejected: no common plane
  contract, so the core could not route or reconcile uniformly.
