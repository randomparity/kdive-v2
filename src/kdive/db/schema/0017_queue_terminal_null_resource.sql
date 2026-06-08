-- 0017_queue_terminal_null_resource.sql — let a never-placed queued row reach `failed`
-- (M1.4, ADR-0069 / #165). Additive to 0016 (forward-only, ADR-0015).
--
-- 0016 made `resource_id` nullable, guarded so NULL is legal only for `requested`/`released`
-- (a queued row that never held a host). The promotion sweep (#165) terminates a queued
-- request that can never be placed: a budget recheck failure at promotion, and a
-- never-placeable request past the max-wait window (queue_timeout) both flip
-- `requested -> failed`. That row never got a `resource_id`, so the 0016 CHECK would reject
-- the terminal. Add `failed` to the NULL-`resource_id` whitelist so a queued request can
-- terminate without ever being stamped with a host. An ever-placed row
-- (granted/active/releasing/expired) still requires a non-NULL `resource_id`.

ALTER TABLE allocations
    DROP CONSTRAINT allocations_resource_id_state_check;
ALTER TABLE allocations
    ADD CONSTRAINT allocations_resource_id_state_check
        CHECK (resource_id IS NOT NULL OR state IN ('requested', 'released', 'failed'));

-- Widen the runs.failure_category / jobs.error_category CHECKs to admit `queue_timeout`,
-- the new ErrorCategory (ADR-0069): a queued request reaped past the max-wait window. It is
-- carried in the promotion-sweep audit args today, but the SQL↔enum tie (tested in
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
        'allocation_denied', 'quota_exceeded', 'lease_expired', 'queue_timeout',
        'provisioning_failure', 'install_failure',
        'transport_failure', 'control_failure',
        'authorization_denied'));
