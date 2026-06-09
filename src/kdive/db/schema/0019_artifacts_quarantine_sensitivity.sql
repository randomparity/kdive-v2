-- 0019_artifacts_quarantine_sensitivity.sql — M1.5 object-store quarantine (ADR-0075).
-- Additive to 0001 (forward-only, ADR-0015). Widens the artifacts.sensitivity CHECK to admit
-- the `quarantined` value — a raw artifact persisted before secret registration completes,
-- excluded from the redacted-only serve gates and healed to a redacted sibling within the op
-- (ADR-0075) — alongside `sensitive`/`redacted`; mirrors Sensitivity in domain/models.py.
-- Drop-and-recreate keeps the constraint name stable for the SQL<->enum tie.
ALTER TABLE artifacts DROP CONSTRAINT artifacts_sensitivity_check;
ALTER TABLE artifacts ADD CONSTRAINT artifacts_sensitivity_check
    CHECK (sensitivity IN ('sensitive', 'redacted', 'quarantined'));
