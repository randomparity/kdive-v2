-- 0016_pending_queue.sql — durable `requested` queue state for capacity-denied
-- requests (M1.4, ADR-0069). Additive to 0015 (forward-only, ADR-0015).
--
-- A capacity-denied `allocations.request` with `on_capacity=queue` rests as a `requested`
-- allocation holding only a queue position: no budget reserve, no lease, no occupancy slot,
-- no resolved device. The host-cap and grant-quota occupancy counters move to a
-- GRANTED/ACTIVE/RELEASING predicate in the service layer (not a schema change) so a queued
-- row never self-blocks its own promotion; `requested` stays non-terminal/live for the lease
-- and reconciler logic.

-- A distinct per-project cap on queued rows, separate from the grant cap
-- (max_concurrent_allocations, which no longer counts `requested`). Backfills existing rows
-- to 0: the queue is opt-in, fail-closed like the other caps — an operator raises it via
-- accounting.set_quota before on_capacity=queue can enqueue.
ALTER TABLE quotas
    ADD COLUMN max_pending_allocations integer NOT NULL DEFAULT 0;

-- resource_id becomes nullable, guarded so NULL is legal only for a queued row that was
-- never placed on a host — its `requested` resting state and its `released` cancellation
-- terminal (a queued row cancelled via allocations.release goes requested -> released while
-- still holding no host, ADR-0069). Mirrors the 0012 audit_log nullable-object CHECK: a NULL
-- can never leak into a granted/active/releasing/expired/failed (i.e. ever-placed) row.
ALTER TABLE allocations
    ALTER COLUMN resource_id DROP NOT NULL;
ALTER TABLE allocations
    ADD CONSTRAINT allocations_resource_id_state_check
        CHECK (resource_id IS NOT NULL OR state IN ('requested', 'released'));

-- The original request inputs a queued row needs to re-admit at promotion (#165). Distinct
-- from pcie_claim (the resolved devices, written only at grant): these are the *requested*
-- specs, the target descriptor, and shape (size is covered by requested_* + requested_disk_gb).
ALTER TABLE allocations
    ADD COLUMN requested_pcie_specs jsonb NOT NULL DEFAULT '[]';
ALTER TABLE allocations
    ADD COLUMN requested_kind text;
ALTER TABLE allocations
    ADD COLUMN requested_resource_id uuid REFERENCES resources (id);

-- Backs the oldest-placeable scan the promotion sweep runs (#165): a partial index over the
-- backlog only, ordered by enqueue time.
CREATE INDEX idx_allocations_requested_created_at
    ON allocations (created_at)
    WHERE state = 'requested';
