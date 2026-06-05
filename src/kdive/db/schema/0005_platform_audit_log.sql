-- 0005_platform_audit_log.sql — platform read-access audit trail (ADR-0043 §4).
-- Additive to 0001/0002 (forward-only, ADR-0015). The per-project `audit_log` cannot
-- represent a project-independent platform action: its `project`/`object_kind`/
-- `object_id` are NOT NULL and `audit.record` enforces `project in ctx.projects`. This
-- append-only table records cross-project reads and platform-role denials without a
-- project-membership guard. `scope` describes the breadth of the read (e.g.
-- 'all-projects' or the project set), not a single project/object. No behavior —
-- `audit.record_platform` is the writer; no tool calls it this round beyond a harness.
CREATE TABLE platform_audit_log (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ts            timestamptz NOT NULL DEFAULT now(),
    principal     text NOT NULL,
    agent_session text,
    -- Nullable: null for an audited granted-set member read that carries no platform
    -- role; set to the granting role for a platform_auditor/platform_admin read.
    platform_role text,
    tool          text NOT NULL,
    scope         text NOT NULL,
    args_digest   text NOT NULL
);
