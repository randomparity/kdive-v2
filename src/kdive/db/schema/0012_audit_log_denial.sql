-- 0012_audit_log_denial.sql — denial-audit retrofit (ADR-0062 §5, issue #142).
-- The dispatch-boundary denial-audit (RoleDenied, member over-reach only) knows the
-- actor/tool/project but NOT the object the handler would have resolved after the gate,
-- so object_kind/object_id become nullable for denial rows. project stays NOT NULL (a
-- member-over-reach denial always carries a resolvable project). A `reason` column
-- captures the human-readable denial reason. The CHECK keeps the original
-- object-present invariant for every real transition; it is keyed on the reserved bare
-- transition literal 'denied', so the destructive gate's `{op}:denied` rows (which
-- always carry their gated object) satisfy the object-present branch unchanged.

ALTER TABLE audit_log ALTER COLUMN object_kind DROP NOT NULL;
ALTER TABLE audit_log ALTER COLUMN object_id   DROP NOT NULL;
ALTER TABLE audit_log ADD COLUMN reason text;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_object_present_unless_denied
    CHECK (transition = 'denied'
           OR (object_kind IS NOT NULL AND object_id IS NOT NULL));
