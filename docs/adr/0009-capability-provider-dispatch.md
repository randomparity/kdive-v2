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

_TBD — to be filled before implementation._

## Consequences

_TBD._

## Alternatives considered

_TBD._
