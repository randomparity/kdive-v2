-- 0020_resources_kind_remote_libvirt.sql — M2 remote-libvirt resource kind (ADR-0076).
-- Additive to 0001 (forward-only, ADR-0015). Widens the resources.kind CHECK to admit the
-- `remote-libvirt` provider kind alongside `local-libvirt` and `fault-inject`; mirrors
-- ResourceKind in domain/models.py and lands with the runtime that registers it (M2
-- foundation), so the CHECK<->registry parity test never sees a CHECK-allowed kind without
-- a buildable runtime. Drop-and-recreate keeps the constraint name stable.
ALTER TABLE resources DROP CONSTRAINT resources_kind_check;
ALTER TABLE resources ADD CONSTRAINT resources_kind_check
    CHECK (kind IN ('local-libvirt', 'fault-inject', 'remote-libvirt'));
