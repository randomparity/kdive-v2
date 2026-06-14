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
- **Spec:** [`../specs/m1.4-system-catalog-scheduling.md`](../design/m1.4-system-catalog-scheduling.md)

## Context

Admission is grant-or-deny synchronously: a request that doesn't fit the host cap or the
project's concurrency quota is denied, and the agent must poll-and-retry by hand. On
scarce hardware this wastes the host — a slot frees and stays idle until some client
happens to retry. The top-level design calls for a reservation / backlog scheduler so
there is "always work queued."

The committed Allocation state machine already has a **`requested`** state
(`requested → granted`, ADR-0023) that admission currently transits through inside one
transaction and never rests in — today `_grant` inserts a row directly as `granted`, so no
row is ever persisted as `requested`. That unused state is the shape of a queued request:
admitted to the backlog, not yet granted.

**But `requested` is not semantically free.** The existing occupancy counters
(`services/allocation_admission.py`) define `_NON_TERMINAL = (REQUESTED, GRANTED, ACTIVE,
RELEASING)` and count it for **both** the per-host cap (`_count_non_terminal`) and the
project grant quota (`_within_alloc_quota`). So persisting queued rows as `requested`
without changing those counters would make a queued request occupy the very slot it is
waiting for — blocking other grants and livelocking its own promotion (the promotion's
capacity replay would count the candidate row itself). Reusing the state therefore requires
a deliberate counter change, made below; this is the load-bearing detail, not an
incidental one.

The full scheduler — priority, preemption, future-window booking, backfill — is a large
policy subsystem with its own ADR series. M1.4 wants the smallest durable surface that
ends the idle-host waste.

## Decision

We will make **`requested` a durable resting state for a queued request** and add a
**work-conserving FIFO promotion sweep** to the reconciler.

**A dedicated occupancy predicate stops counting `requested` (the load-bearing change).**
The host cap and the grant quota switch to an **occupancy** predicate of exactly
**`GRANTED / ACTIVE / RELEASING`**. This is **not** a redefinition of "non-terminal" or
"live": `requested` remains a non-terminal, live state, and everything that reasons about
liveness — the ADR-0036 lease-expiry sweep, the reconciler's orphan/leaked-infra
detection, the release path — must still see a queued row as live. So the change introduces
a separate occupancy predicate for the two capacity counts; it does **not** edit the shared
`_NON_TERMINAL` liveness constant in place (which would wrongly tell that logic a queued row
is gone). A queued row therefore occupies neither a host slot nor a grant-quota slot, the
promotion's capacity replay never counts the candidate against itself, and the expiry/
reconciler logic still treats it as live. Queued depth is bounded separately (see
Consequences), not by the grant cap.

**Enqueue.** `allocations.request` gains `on_capacity: "queue" | "deny"` (default `deny`,
today's behavior). On a **capacity** denial — the host cap or the concurrency quota — with
`on_capacity=queue`, the request is inserted as a `requested` allocation holding **only a
queue position**: no budget reserve, no lease window, no occupancy slot. The queued row
**persists the original request inputs** — size/selector, `window`, the target (by kind or
by id), the shape (ADR-0067), the PCIe device union (ADR-0068), and the `idempotency_key` —
and leaves **`resource_id` null** (it is nullable only in `requested`; required from
`granted` on). The host is *not* frozen at enqueue. **Budget and configuration denials still
hard-deny** — waiting will not free budget, and a malformed request is malformed forever.
The enqueue **records and honors the `idempotency_key`** exactly as a grant does: a
replayed queued request returns the existing `requested` allocation, never a second
enqueue. The **`max_pending_allocations` check and the `requested`-row insert are atomic
under the PROJECT lock** — the same lock the grant's check-then-debit uses (ADR-0040) — so
two concurrent `on_capacity=queue` requests cannot both pass the cap and overshoot it.

**Promotion.** The reconciler's sweep is **work-conserving**: it **re-runs selection** for
each queued request from its persisted inputs — PCIe-aware host choice (ADR-0068),
cordon-skipping, exactly as a fresh admission — and per resource promotes the **oldest
*placeable* request**, not strictly the global oldest, so a free host is never idled
because the single oldest request waits on a *different* busy host (and a by-kind or
PCIe-aware request can land on whichever matching host frees first, never a host frozen at
enqueue). Per candidate, under `PROJECT → RESOURCE`, it replays the existing
check-then-debit; on success it **stamps `resource_id`**, transitions `requested →
granted`, writes the `reserved` ledger debit, and sets the lease window (ADR-0036) — the
same grant `admit` performs today.

**Exit.** A queued request is cancelled via `allocations.release`. This adds a
`requested → released` edge to the state machine + guard table (today nothing rests in
`requested`, so no exit edge exists). Because a queued row was **never reserved**, releasing
it writes the audit row but **no ledger entry** — there is no `reserved` debit to reverse,
so the release path skips the `reconciled` credit and the `active_ended_at` stamp entirely;
writing a credit would mint a spurious negative delta (the budget-minting hazard ADR-0007
§2 guards). A request that stays
`requested` (never placeable — e.g. its only host went permanently offline) past a
configurable **max-wait window** is reaped by the sweep to `failed` with a **distinct
`queue_timeout` reason** — *not* `lease_expired`: a queued request never held a lease, and
ADR-0036/0021 reserve `lease_expired` for a *granted* lease window elapsing, so conflating
them would corrupt that SLO signal. `queue_timeout` is a new `ErrorCategory` sibling (the
spec's error-taxonomy note is updated). The reaper mirrors the idle-Investigation sweep and
ensures an unplaceable request cannot pin pending capacity forever (a queued row has no
`lease_expiry`, so the ADR-0036 expiry sweep would otherwise never touch it). M1.4 adds
**no** priority, preemption, or future-window booking.

## Consequences

- A freed slot is filled by the reconciler on its next pass instead of waiting for a
  client retry; the host stays busy while work is backlogged.
- Budget is charged at grant, never at enqueue — a request that waits and is cancelled
  costs nothing, and the ledger never holds a reserve for an ungranted claim.
- The promotion replays the **whole** gate, so a request that fit on capacity but has since
  gone over budget is correctly denied at promotion (transitioned to `failed`), not
  granted on stale admission. This is a deliberate asymmetry: capacity contention makes a
  request **wait**, but a budget recheck failure at promotion **terminates** it (rather than
  re-queuing indefinitely on a claim that may never clear) — the agent re-requests once
  budget recovers. A request reaches promotion only because it passed budget at enqueue, so
  this fires only when intervening spend consumed the headroom.
- The sweep runs under the service identity but attributes the grant audit row to the
  queued allocation's original `(principal, agent_session)`, so the backlog grant is
  indistinguishable in audit from a synchronous one.
- A **distinct** per-project pending cap — `max_pending_allocations`, a new `quotas` column
  (not the grant cap `max_concurrent_allocations`, which no longer counts `requested` rows)
  — bounds enqueue depth so one project cannot flood the backlog; over the cap,
  `on_capacity=queue` itself is denied.
- The migration adds `max_pending_allocations` to `quotas`, makes `allocations.resource_id`
  **nullable guarded by a CHECK** (`resource_id` may be NULL only when `state =
  'requested'`, mirroring the M1.3 `audit_log` nullable-object CHECK, ADR-0062 §5, so a null
  can never leak into a `granted`/active row), adds the persisted request-input columns the
  queued row needs to re-admit (selector/target/shape/PCIe union; the existing `requested_*`
  snapshot covers size), and a partial index `(created_at) WHERE state = 'requested'` for the
  oldest-placeable scan; it switches the occupancy-count queries to the
  `GRANTED/ACTIVE/RELEASING` predicate and adds the `requested → released` edge. No new state
  value or table is needed (the `queue_timeout` reason is an `ErrorCategory`, not a state).
- Because promotion is work-conserving, head-of-line unfairness is bounded to a **single
  contended host**: a large request can wait behind smaller ones for *that* host, but never
  blocks promotion onto a *different* free host. There is no future-window guarantee —
  accepted for M1.4 and the explicit upgrade path to the full scheduler.

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
