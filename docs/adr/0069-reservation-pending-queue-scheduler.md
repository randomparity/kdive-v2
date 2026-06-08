# ADR 0069 — Reservation / FIFO pending-queue scheduler (M1.4)

- **Status:** Proposed
- **Date:** 2026-06-08
- **Deciders:** platform maintainers
- **Builds on (does not supersede):** [ADR-0023](0023-discovery-allocation-admission.md)
  (the `requested → granted` admission path and the per-host capacity check),
  [ADR-0007](0007-metering-budgets-admission.md) (the budget/quota check-then-debit the
  promotion replays), [ADR-0036](0036-reservation-lease-semantics.md) (the lease window a
  promotion acquires), [ADR-0040](0040-admission-lifecycle-concurrency.md) (the
  `PROJECT → RESOURCE` lock order the sweep reuses),
  [ADR-0021](0021-reconciler-loop-drift-repair.md) (the reconciler loop the sweep joins),
  [ADR-0062](0062-platform-operations.md) (the `cordoned` schedulability flag placement
  honors).
- **Spec:** [`../specs/m1.4-system-catalog-scheduling.md`](../specs/m1.4-system-catalog-scheduling.md)

## Context

Admission is grant-or-deny synchronously: a request that doesn't fit the host cap or the
project's concurrency quota is denied, and the agent must poll-and-retry by hand. On
scarce hardware this wastes the host — a slot frees and stays idle until some client
happens to retry. The top-level design calls for a reservation / backlog scheduler so
there is "always work queued."

The committed Allocation state machine already has a **`requested`** state
(`requested → granted`, ADR-0023) that admission currently transits through inside one
transaction and never rests in. That is exactly the shape of a queued request: admitted to
the backlog, not yet granted.

The full scheduler — priority, preemption, future-window booking, backfill — is a large
policy subsystem with its own ADR series. M1.4 wants the smallest durable surface that
ends the idle-host waste.

## Decision

We will make **`requested` a durable resting state for a queued request** and add a
**FIFO promotion sweep** to the reconciler. `allocations.request` gains
`on_capacity: "queue" | "deny"` (default `deny`, today's behavior). On a **capacity**
denial — the host cap or the concurrency quota — with `on_capacity=queue`, the request is
inserted as a `requested` allocation holding **only a queue position**: no budget reserve,
no lease window, no capacity slot consumed. **Budget and configuration denials still
hard-deny** — waiting will not free budget, and a malformed request is malformed forever.
The reconciler's promotion sweep selects `requested` allocations oldest-first and, per
allocation under `PROJECT → RESOURCE`, replays the existing check-then-debit; on success it
transitions `requested → granted`, writes the `reserved` ledger debit, and sets the lease
window (ADR-0036) — the same grant `admit` performs today. Placement skips `cordoned`
hosts. A queued request is cancelled via `allocations.release`. M1.4 adds **no** priority,
preemption, or future-window booking.

## Consequences

- A freed slot is filled by the reconciler on its next pass instead of waiting for a
  client retry; the host stays busy while work is backlogged.
- Budget is charged at grant, never at enqueue — a request that waits and is cancelled
  costs nothing, and the ledger never holds a reserve for an ungranted claim.
- The promotion replays the **whole** gate, so a request that fit on capacity but has since
  gone over budget is correctly denied at promotion (transitioned to `failed`), not
  granted on stale admission.
- The sweep runs under the service identity but attributes the grant audit row to the
  queued allocation's original `(principal, agent_session)`, so the backlog grant is
  indistinguishable in audit from a synchronous one.
- A per-project pending cap (reusing the quota row) bounds enqueue depth so one project
  cannot flood the backlog; over the cap, `on_capacity=queue` itself is denied.
- The migration adds a partial index `(created_at) WHERE state = 'requested'` for the FIFO
  scan; no new state value or table is needed.
- FIFO can be unfair to a large request behind small ones (head-of-line on capacity), and
  has no future-window guarantee — accepted for M1.4 and the explicit upgrade path to the
  full scheduler.

## Alternatives considered

- **Full scheduler now** (priority, preemption, future windows, backfill). Maximally
  capable, but a multi-PR policy subsystem with its own ADR series; rejected for M1.4 in
  favor of the minimal queue that ends the idle-host waste.
- **A separate `reservations` table** distinct from `allocations`. Cleaner conceptual
  split, but duplicates the attribution, lock, idempotency, and audit machinery a queued
  request needs and forces a hand-off at promotion; rejected — `requested` already models
  "admitted, not yet granted," and reusing it keeps one object and one lifecycle.
- **Charge budget at enqueue.** Would let the queue reflect committed spend, but holds a
  reserve for a claim that may never grant and lets a cancelled wait cost money; rejected —
  the ledger reserves at grant, where the lease starts.
- **Add a persisted `draining` status to feed the scheduler.** The top-level design names
  it, but `cordoned` already means "placement skips," which is all the sweep needs;
  rejected — no separate status, `draining` stays unrealized.
