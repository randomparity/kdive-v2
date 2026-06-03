# ADR 0008 — Async worker tier + durable job queue

- **Status:** Proposed
- **Date:** 2026-06-03
- **Implements core decision:** #8 in [`../specs/top-level-design.md`](../specs/top-level-design.md)

## Context

Long-running ops (provision, build, install, capture-vmcore — 30+ minutes) become
durable jobs pulled by a worker tier whose pools are scoped per resource class.
Jobs carry a worker heartbeat/lease so the reconciler can detect dead workers.
Hard per-tenant sandboxing is designed-for but deferred. Concrete queue technology
(Postgres-backed vs Redis/Celery vs Temporal) is an open follow-up. See the spec's
"System topology" and "Reconciliation & teardown".

## Decision

_TBD — to be filled before implementation._

## Consequences

_TBD._

## Alternatives considered

_TBD._
