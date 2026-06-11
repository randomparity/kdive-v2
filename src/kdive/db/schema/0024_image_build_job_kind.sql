-- 0024_image_build_job_kind.sql — M2.4 image-build job kind (ADR-0092, #285).
-- Additive to 0003 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit the
-- `image_build` op (kdivectl images build/publish enqueues it); mirrors JobKind in
-- domain/models.py. Drop-and-recreate keeps the constraint name stable for the SQL<->enum
-- tie (tested in test_migrate.py). This does NOT touch image_catalog (owned whole by 0023).
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build'));
