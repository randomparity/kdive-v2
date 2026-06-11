-- 0022_egress_probe_guests.sql — reaper-visible markers for doctor egress-probe guests
-- (M2.3, ADR-0091 §3). Additive to 0021 (forward-only, ADR-0015).
--
-- The `guest_egress` doctor check provisions a tiny ephemeral guest on the target provider
-- and execs a presigned HEAD/PUT against object-store from inside it. Provisioning real,
-- cost-bearing infrastructure means teardown can fail (the exec hangs, doctor is interrupted,
-- the worker dies), so each probe guest is registered here under an owned, reaper-visible
-- marker carrying an active-run heartbeat (`heartbeat_at`) and a hard TTL (`ttl_deadline`).
--
-- `reconciler/provider_reaping` honors the heartbeat: a probe whose owning doctor run is still
-- beating (heartbeat fresh) is NEVER reaped, and one whose run has stopped beating (a leak) is
-- reaped, with the `ttl_deadline` as a backstop sized well above the probe's max runtime so the
-- reaper never destroys a guest mid-check. The single-flight coordinator keys on `provider`, so
-- a partial unique index enforces at most one live probe row per provider.
CREATE TABLE egress_probe_guests (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider     text        NOT NULL,
    domain_name  text        NOT NULL UNIQUE,
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    ttl_deadline timestamptz NOT NULL,
    released_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- At most one live (not-yet-released) probe per provider: the DB-level single-flight fence.
CREATE UNIQUE INDEX egress_probe_guests_one_live_per_provider
    ON egress_probe_guests (provider)
    WHERE released_at IS NULL;
