-- 0015_allocation_system_shape.sql — M1.4 sizing-snapshot identity (ADR-0067, #161).
-- Additive to 0014 (forward-only, ADR-0015). Records the resolved-sizing snapshot identity
-- on the existing snapshot columns rather than minting parallel sizing columns: an
-- Allocation already carries requested_vcpus / requested_memory_gb (the at-grant snapshot,
-- ADR-0007 §3) and a System carries its full provisioning_profile JSON (vcpu / memory_mb /
-- disk_gb). This migration adds only the nullable `shape` name label to each table (NULL for
-- a full-custom request) plus `requested_disk_gb` on allocations, where the existing snapshot
-- falls short. `shape` is a recorded LABEL, not a foreign key to system_shapes (ADR-0067) —
-- so a later shapes.delete never FK-blocks and a shapes.set never retroactively re-sizes a
-- stamped row (availability/reuse read sizing from this persisted state, not the catalog).

ALTER TABLE allocations ADD COLUMN shape text;
ALTER TABLE allocations ADD COLUMN requested_disk_gb integer
    CONSTRAINT allocations_requested_disk_positive_check CHECK (requested_disk_gb > 0);
ALTER TABLE systems ADD COLUMN shape text;
