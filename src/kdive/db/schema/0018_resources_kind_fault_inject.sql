-- 0018_resources_kind_fault_inject.sql — M1.5 fault-injection resource kind (ADR-0071).
-- Additive to 0001 (forward-only, ADR-0015). Widens the resources.kind CHECK to admit the
-- `fault-inject` mock provider kind alongside `local-libvirt`; mirrors ResourceKind in
-- domain/models.py and lands with the runtime that registers it (M1.5 issue 2), so the
-- CHECK<->registry parity test never sees a CHECK-allowed kind without a buildable runtime.
-- Drop-and-recreate keeps the constraint name stable for the SQL<->enum tie.
ALTER TABLE resources DROP CONSTRAINT resources_kind_check;
ALTER TABLE resources ADD CONSTRAINT resources_kind_check
    CHECK (kind IN ('local-libvirt', 'fault-inject'));
