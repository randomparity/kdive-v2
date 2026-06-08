-- 0011_ops_control.sql — single-row control-plane flags for platform ops (ADR-0062).
-- Additive, forward-only (ADR-0015). `ops_control` holds the worker's `queue_paused`
-- flag: `ops.queue_pause`/`ops.queue_resume` (platform_operator) toggle it, and the
-- worker reads it before each `dequeue`. The single-row guard is a fixed boolean PK
-- pinned to `true` (`singleton`), so only one row can ever exist; the seed row is
-- inserted here so a fresh deploy reads `queue_paused = false` without a tool write.
CREATE TABLE ops_control (
    singleton    boolean PRIMARY KEY DEFAULT true
        CONSTRAINT ops_control_singleton_check CHECK (singleton = true),
    queue_paused boolean NOT NULL DEFAULT false,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER ops_control_set_updated_at BEFORE UPDATE ON ops_control
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();
INSERT INTO ops_control (singleton) VALUES (true);
