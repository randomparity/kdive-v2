-- 0028_capacity_exhausted_category.sql — build-host capacity failure category (#342, ADR-0099).
-- Additive to 0027 (forward-only, ADR-0015). Widens the runs.failure_category and
-- jobs.error_category CHECKs to admit `capacity_exhausted`, the new ErrorCategory value a
-- build-host-at-capacity admission returns. It is raised synchronously at the runs.build
-- boundary (so it is not currently persisted on a Run/Job), but the SQL↔enum tie (tested in
-- test_migrate.py) requires every ErrorCategory value be admitted by these constraints.
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
        'authorization_denied', 'capacity_exhausted'));

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
        'authorization_denied', 'capacity_exhausted'));
