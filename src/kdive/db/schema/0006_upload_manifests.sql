-- 0006_upload_manifests.sql — owner-scoped upload manifests for external ingestion
-- (ADR-0048 §4/§6). Additive, forward-only (ADR-0015). One row per in-flight upload
-- owner (a CREATED Run or a DEFINED System); holds the declared (name, sha256,
-- size_bytes) set complete_build compares stored objects against, the object-key prefix
-- the reaper lists, and the deadline the reaper keys off. The row is replaced on a
-- re-mint (one call, full set) and deleted when the owner finalizes or is reaped. It is
-- NOT the write-once artifacts row — no artifacts-row state changes.
CREATE TABLE upload_manifests (
    owner_kind text NOT NULL CONSTRAINT upload_manifests_owner_kind_check
                   CHECK (owner_kind IN ('runs', 'systems')),
    owner_id   uuid NOT NULL,
    prefix     text NOT NULL,
    manifest   jsonb NOT NULL,
    deadline   timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT upload_manifests_pkey PRIMARY KEY (owner_kind, owner_id)
);
CREATE TRIGGER upload_manifests_set_updated_at BEFORE UPDATE ON upload_manifests
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();
-- The reaper scans WHERE deadline < now() (ADR-0048 §6); index the scan column.
CREATE INDEX upload_manifests_deadline_idx ON upload_manifests (deadline);
