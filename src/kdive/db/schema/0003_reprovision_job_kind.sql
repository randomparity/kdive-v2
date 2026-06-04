-- 0003_reprovision_job_kind.sql — M1 reprovision-in-place job kind (ADR-0038).
-- Additive to 0002 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit the
-- `reprovision` op (systems.reprovision enqueues it); mirrors JobKind in domain/models.py.
-- No behavior — issue ⑦ wires systems.reprovision and the handler. Drop-and-recreate
-- keeps the constraint name stable for the SQL↔enum tie (tested in test_migrate.py).
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore'));
