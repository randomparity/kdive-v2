-- ADR-0064: Run-scoped expected boot failure metadata for expected-crash reproduction.

ALTER TABLE runs
    ADD COLUMN expected_boot_failure jsonb,
    ADD CONSTRAINT runs_expected_boot_failure_object_check
        CHECK (
            expected_boot_failure IS NULL
            OR jsonb_typeof(expected_boot_failure) = 'object'
        );
