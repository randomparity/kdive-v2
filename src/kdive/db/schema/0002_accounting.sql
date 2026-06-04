-- 0002_accounting.sql — M1 accounting/admission data layer (ADR-0007, ADR-0040).
-- Additive to 0001 (forward-only, ADR-0015). New tables for the cost model, metering
-- ledger, budgets/quotas, and request-idempotency; widened allocation/system state
-- CHECKs; allocation size + billing-interval columns. No behavior — issues ②–⑤ wire
-- the writers. kcu values are `numeric` so cost arithmetic stays exact.

-- The only per-cost_class knob (ADR-0007 §1); seeded with the local baseline.
CREATE TABLE cost_class_coefficients (
    cost_class text PRIMARY KEY,
    coeff      numeric NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER cost_class_coefficients_set_updated_at
    BEFORE UPDATE ON cost_class_coefficients
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();
INSERT INTO cost_class_coefficients (cost_class, coeff) VALUES ('local', 1.0);

-- Per-project spend budget with the O(1) running spent total (ADR-0007 §3): every
-- ledger write adjusts spent_kcu in the same transaction under the project lock, so
-- admission reads budget_remaining = limit_kcu - spent_kcu without summing the ledger.
CREATE TABLE budgets (
    project    text PRIMARY KEY,
    limit_kcu  numeric NOT NULL,
    spent_kcu  numeric NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER budgets_set_updated_at BEFORE UPDATE ON budgets
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

-- Per-project concurrency caps (ADR-0007 §4): allocations checked at request,
-- systems at provision. No row → the project is denied (no silent default).
CREATE TABLE quotas (
    project                    text PRIMARY KEY,
    max_concurrent_allocations integer NOT NULL,
    max_concurrent_systems     integer NOT NULL,
    updated_at                 timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER quotas_set_updated_at BEFORE UPDATE ON quotas
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

-- Append-only, signed metering ledger (ADR-0007 §3): the audit trail and the
-- by_cost_class source for accounting.usage. resource_id is nullable for a credit
-- reconciling an allocation released before any System was provisioned.
CREATE TABLE ledger (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ts            timestamptz NOT NULL DEFAULT now(),
    project       text NOT NULL,
    allocation_id uuid NOT NULL REFERENCES allocations (id),
    resource_id   uuid REFERENCES resources (id),
    cost_class    text NOT NULL,
    event_type    text NOT NULL CONSTRAINT ledger_event_type_check
                      CHECK (event_type IN ('reserved', 'reconciled')),
    kcu_delta     numeric NOT NULL,
    note          text
);
CREATE INDEX ledger_project_idx ON ledger (project);
CREATE INDEX ledger_allocation_id_idx ON ledger (allocation_id);

-- Synchronous request/renew retry-dedup (ADR-0040 §3), scoped per principal: the PK
-- is (principal, key) so one tenant's client-chosen key cannot resolve another's
-- allocation (a global key namespace would be a cross-tenant disclosure bug).
CREATE TABLE idempotency_keys (
    key        text NOT NULL,
    principal  text NOT NULL,
    project    text NOT NULL,
    kind       text NOT NULL,
    result     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal, key)
);

-- Allocation size (rate inputs persisted at grant) and billing interval. active_hours
-- = active_ended_at - active_started_at, never derived from updated_at (ADR-0007 §3).
ALTER TABLE allocations ADD COLUMN requested_vcpus integer;
ALTER TABLE allocations ADD COLUMN requested_memory_gb integer;
ALTER TABLE allocations ADD COLUMN active_started_at timestamptz;
ALTER TABLE allocations ADD COLUMN active_ended_at timestamptz;

-- Widen the lifecycle CHECKs to mirror the M1 state.py edges: allocation gains the
-- terminal `expired` (reconciler sweep), system gains `reprovisioning` (reprovision
-- in place). Drop-and-recreate keeps the constraint name stable for the SQL↔enum tie.
ALTER TABLE allocations DROP CONSTRAINT allocations_state_check;
ALTER TABLE allocations ADD CONSTRAINT allocations_state_check
    CHECK (state IN ('requested', 'granted', 'active',
                     'releasing', 'released', 'expired', 'failed'));

ALTER TABLE systems DROP CONSTRAINT systems_state_check;
ALTER TABLE systems ADD CONSTRAINT systems_state_check
    CHECK (state IN ('defined', 'provisioning', 'ready', 'reprovisioning',
                     'crashed', 'torn_down', 'failed'));
