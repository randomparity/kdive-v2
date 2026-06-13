-- 0026_not_found_conflict_categories.sql — object-lookup failure categories (#338, ADR-0097).
-- Additive to 0025 (forward-only, ADR-0015). Widens the runs.failure_category and
-- jobs.error_category CHECKs to admit `not_found` and `conflict`, the new ErrorCategory
-- values. `not_found` is emitted by object-lookup tools for a syntactically valid but absent
-- (or ungranted, no-leak) id; `conflict` is reserved for a uniqueness/state conflict and is
-- defined-but-unemitted for now. The SQL↔enum tie (tested in test_migrate.py) requires every
-- ErrorCategory value be admitted by these constraints even before a tool persists it.
-- Drop-and-recreate keeps the constraint names stable. Mirrors ErrorCategory in
-- domain/errors.py.
ALTER TABLE runs DROP CONSTRAINT runs_failure_category_check;
ALTER TABLE runs ADD CONSTRAINT runs_failure_category_check
    CHECK (failure_category IN (
        'configuration_error', 'missing_dependency',
        'build_failure', 'boot_timeout', 'readiness_failure',
        'debug_attach_failure', 'infrastructure_failure',
        'stale_handle', 'transport_conflict', 'not_implemented',
        'not_found', 'conflict',
        'allocation_denied', 'quota_exceeded', 'lease_expired', 'queue_timeout',
        'provisioning_failure', 'install_failure',
        'transport_failure', 'control_failure',
        'authorization_denied'));

ALTER TABLE jobs DROP CONSTRAINT jobs_error_category_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_error_category_check
    CHECK (error_category IN (
        'configuration_error', 'missing_dependency',
        'build_failure', 'boot_timeout', 'readiness_failure',
        'debug_attach_failure', 'infrastructure_failure',
        'stale_handle', 'transport_conflict', 'not_implemented',
        'not_found', 'conflict',
        'allocation_denied', 'quota_exceeded', 'lease_expired', 'queue_timeout',
        'provisioning_failure', 'install_failure',
        'transport_failure', 'control_failure',
        'authorization_denied'));
