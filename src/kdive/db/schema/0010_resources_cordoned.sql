-- 0010_resources_cordoned.sql — host schedulability flag (M1.3, ADR-0062 §3).
-- `cordoned` is an axis orthogonal to the health `status` enum: a host can be
-- `cordoned` AND `degraded`/`offline`, and the two columns never clobber each other.
-- Placement (allocations._resolve_resource) skips a cordoned/non-available host on
-- the pick-by-kind path and rejects one named by explicit id.

ALTER TABLE resources
    ADD COLUMN cordoned boolean NOT NULL DEFAULT false;
