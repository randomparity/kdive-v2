-- Additive to 0006. Durable failed jobs keep a redacted, string-only failure context
-- so jobs.get/jobs.wait can explain the failure without exposing logs or secrets.
ALTER TABLE jobs
    ADD COLUMN failure_context jsonb NOT NULL DEFAULT '{}'::jsonb;
