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

_TBD — to be filled before implementation._

## Consequences

_TBD._

## Alternatives considered

_TBD._
