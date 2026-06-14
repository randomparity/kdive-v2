# ADR 0008 — Async worker tier + durable job queue

- **Status:** Proposed
- **Date:** 2026-06-03
- **Implements core decision:** #8 in [`../specs/top-level-design.md`](../design/top-level-design.md)

## Context

Long-running ops (provision, build, install, capture-vmcore — 30+ minutes) become
durable jobs pulled by a worker tier whose pools are scoped per resource class.
Jobs carry a worker heartbeat/lease so the reconciler can detect dead workers.
Hard per-tenant sandboxing is designed-for but deferred. Concrete queue technology
(Postgres-backed vs Redis/Celery vs Temporal) is an open follow-up. See the spec's
"System topology" and "Reconciliation & teardown".

## Decision

Long-running operations (provision, build, install, capture-vmcore) run as
**durable jobs in a Postgres-backed queue** dequeued with
`SELECT … FOR UPDATE SKIP LOCKED`. A job row carries `state`, payload, `attempt`,
`max_attempts`, and a **worker heartbeat/lease**. **Retries are bounded**: a job
that exceeds `max_attempts` — or whose declared op is non-idempotent and crashed
mid-effect — moves to a terminal `failed` state with a dead-letter record rather
than retrying forever. A **lapsed lease** (missed heartbeats) returns the job to
the queue for a remaining attempt, and any partial effect is reconciled via the
op's cleanup contract ([0009](0009-capability-provider-dispatch.md)). **Worker
pools are scoped per resource class** so a flaky pool cannot starve another, and
**within a pool dequeue is fair across tenants** so no single principal/project
monopolizes a resource-class pool. Fast operations (set breakpoint, read memory,
power state) run synchronously. Every long-running tool returns the same
`{job_id, status}` handle, polled via `jobs.get` / `jobs.wait`.

## Consequences

- No new infrastructure — the queue reuses the Postgres mandated by
  [0005](0005-postgres-object-store-state.md), and jobs share its transactions.
- At-least-once delivery; idempotent steps (`run_id` + step) make retries safe
  after a worker dies. Retries are **bounded** — a poison job reaches
  `max_attempts` and is dead-lettered to `failed`, surfaced by `jobs.list` for
  triage, rather than looping forever.
- The heartbeat/lease lets the reconciler mark abandoned jobs and run their
  declared compensation (the cancel/cleanup contract from
  [0009](0009-capability-provider-dispatch.md)).
- Throughput is bounded by Postgres, acceptable at M0/M1 scale; a higher-throughput
  broker can replace the queue behind the same job interface if a milestone needs
  it.

## Alternatives considered

- **Redis + Celery / RQ.** Rejected for M0: extra infrastructure and operations for
  throughput M0 does not need.
- **Temporal or a durable-workflow engine.** Rejected: powerful but heavy; per-op
  compensation plus the reconciler cover M0's durability needs without it.
- **In-process threads / asyncio tasks.** Rejected: not durable across worker
  death — a 30-minute provision must survive a restart.
