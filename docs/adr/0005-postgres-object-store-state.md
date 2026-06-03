# ADR 0005 — Postgres + object store for state; advisory locks replace flock

- **Status:** Proposed
- **Date:** 2026-06-03
- **Implements core decision:** #5 in [`../specs/top-level-design.md`](../specs/top-level-design.md)

## Context

Postgres becomes the system-of-record for structured state and accounting/audit
ledgers; an S3-compatible object store holds bulk artifacts (vmcores, build
outputs, transcripts) referenced by row. Postgres advisory locks replace the
PoC's `flock`/`O_CREAT|O_EXCL` file locks, with a per-project budget-scope lock
for admission control. See the spec's "Cross-cutting concerns" (Concurrency,
Reconciliation & teardown).

## Decision

_TBD — to be filled before implementation._

## Consequences

_TBD._

## Alternatives considered

_TBD._
