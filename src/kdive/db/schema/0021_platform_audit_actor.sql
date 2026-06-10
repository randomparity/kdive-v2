-- 0021_platform_audit_actor.sql — operator-CLI audit attribution (ADR-0089).
-- Additive to 0001 (forward-only, ADR-0015). Adds `actor` classifying the caller class
-- (operator-cli | agent | unknown), resolved server-side from the OIDC client_id/azp claim
-- via security/authz/actor.py. NOT NULL with a default so the column is total over every
-- row; existing rows predate the CLI and are backfilled to 'agent'.
ALTER TABLE platform_audit_log
    ADD COLUMN actor TEXT NOT NULL DEFAULT 'agent';
