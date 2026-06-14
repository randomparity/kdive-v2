# ADR 0005 — Postgres + object store for state; advisory locks replace flock

- **Status:** Proposed
- **Date:** 2026-06-03
- **Implements core decision:** #5 in [`../specs/top-level-design.md`](../design/top-level-design.md)

## Context

Postgres becomes the system-of-record for structured state and accounting/audit
ledgers; an S3-compatible object store holds bulk artifacts (vmcores, build
outputs, transcripts) referenced by row. Postgres advisory locks replace the
PoC's `flock`/`O_CREAT|O_EXCL` file locks, with a per-project budget-scope lock
for admission control. See the spec's "Cross-cutting concerns" (Concurrency,
Reconciliation & teardown).

## Decision

**Postgres is the system-of-record** for all structured state and the
audit/accounting ledgers; an **S3-compatible object store** holds bulk artifacts
(vmcores, build outputs, console/gdb transcripts) referenced by row (key + etag).
Concurrency uses **transaction-scoped Postgres advisory locks**
(`pg_advisory_xact_lock`) — per-Allocation and per-System serialization, plus a
per-project budget-scope lock for admission control — replacing the PoC's
`flock`/`O_CREAT|O_EXCL`. Transaction scope keeps the locks correct behind a
transaction-pooling connection pooler (PgBouncer); session-scoped locks are not
used. Idempotent steps are enforced by a `(run_id, step)` unique key.

## Consequences

- One transactional store gives an atomic check-then-debit for admission control
  ([0007](0007-metering-budgets-admission.md)) and consistent state transitions.
- GB-scale vmcores stay out of Postgres, in the object store with their own
  layout and retention ([0013](0013-object-store-layout-retention.md)).
- Advisory locks auto-release on connection close — they protect *rows*, not
  *infrastructure* — so a reconciler is still required to clean leaked provider
  infra (see the spec's "Reconciliation & teardown").
- A row reference and its object live in two stores, so the write is not atomic:
  the object is written **before** the referencing row commits, the reconciler
  garbage-collects objects with no committed referrer, and a row whose object is
  missing surfaces as `stale_handle`.
- Operational dependency on a managed Postgres and an S3-compatible store.

## Alternatives considered

- **Keep per-run JSON + flock.** Rejected by core decision #5: no durability, no
  multi-user serialization, no transactional ledger.
- **Postgres-only with `bytea` blobs.** Rejected: vmcores are gigabytes; large
  objects belong in an object store.
- **An external lock service (etcd/ZooKeeper).** Rejected: extra infrastructure
  for locking that Postgres advisory locks already provide.
