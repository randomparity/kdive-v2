# ADR 0007 — Metering + budgets/quotas with an admission-control gate

- **Status:** Proposed
- **Date:** 2026-06-03
- **Implements core decision:** #7 in [`../specs/top-level-design.md`](../specs/top-level-design.md)

## Context

A metering ledger plus enforced budgets/quotas gates allocation via admission
control on `allocations.request`. The budget check and ledger debit must be atomic
under a per-project lock so concurrent requests cannot overspend; "always-yes"
local allocation is still capacity-admitted. This ADR also owns the **cost model**:
the normalized reference unit, the per-`cost_class` coefficients, and the
`cost_class` assignment per Resource — so an Investigation's cost rollup across
allocations and cost_classes (local VM + cloud + bare metal) is a meaningful sum.
See the spec's "Allocation", "Investigation", and "Cross-cutting concerns"
(Accounting ledger, Concurrency).

## Decision

_TBD — to be filled before implementation._

## Consequences

_TBD._

## Alternatives considered

_TBD._
