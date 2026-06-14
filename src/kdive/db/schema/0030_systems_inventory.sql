-- 0030_systems_inventory.sql — schema for ADR-0112 (systems.toml inventory).
-- Authors ALL schema the four-phase M2.6 design needs (additive, forward-only, ADR-0015).
-- This is the single-migration-owner: no later phase adds a migration; they only
-- populate/read these columns.

-- managed_by partitions row ownership on every reconciled table so declarative bring-up
-- (config), discovery, and imperative agent tools (runtime) own disjoint row-sets.
ALTER TABLE image_catalog ADD COLUMN managed_by text NOT NULL DEFAULT 'runtime'
    CONSTRAINT image_catalog_managed_by_check
    CHECK (managed_by IN ('config', 'discovery', 'runtime'));
ALTER TABLE resources ADD COLUMN managed_by text NOT NULL DEFAULT 'runtime'
    CONSTRAINT resources_managed_by_check
    CHECK (managed_by IN ('config', 'discovery', 'runtime'));
ALTER TABLE build_hosts ADD COLUMN managed_by text NOT NULL DEFAULT 'runtime'
    CONSTRAINT build_hosts_managed_by_check
    CHECK (managed_by IN ('config', 'discovery', 'runtime'));

-- Staged images: a registered image may carry a provider volume instead of an S3 object_key.
ALTER TABLE image_catalog ADD COLUMN volume text;
-- Relax the original image_object_present CHECK (0023): a non-'defined' row must have
-- EXACTLY ONE of object_key / volume; a 'defined' row (metadata seeded, no image yet) has
-- neither.
ALTER TABLE image_catalog DROP CONSTRAINT image_object_present;
ALTER TABLE image_catalog ADD CONSTRAINT image_object_present CHECK (
    (state = 'defined' AND object_key IS NULL AND volume IS NULL)
    OR (state <> 'defined' AND (object_key IS NULL) <> (volume IS NULL))
);

-- Resource stable identity: a mutable unique name (the id UUID stays the PK/FK target).
-- The unique index is partial so many NULL-name discovered rows coexist; only declared
-- names are uniqueness-constrained, scoped per kind.
ALTER TABLE resources ADD COLUMN name text;
CREATE UNIQUE INDEX resources_kind_name_key ON resources (kind, name) WHERE name IS NOT NULL;

-- Per-project affinity: NULL owner_project = global (any project). owner_project + the
-- allowlist scope a resource to specific projects.
ALTER TABLE resources ADD COLUMN owner_project text;
ALTER TABLE resources ADD COLUMN affinity_allowlist text[] NOT NULL DEFAULT '{}';

-- Lease for runtime-registered resources (leak reaping). NULL for config/discovery rows.
ALTER TABLE resources ADD COLUMN lease_expires_at timestamptz;

-- Backfill ownership for pre-existing rows (load-bearing, see plan Task 1.1 backfill note):
-- Discovered hosts must never be pruned on the first reconcile (not yet declared in the file).
UPDATE resources SET managed_by = 'discovery';
-- ONLY the public baseline catalog is config-equivalent: reconcile then fully owns it (a
-- previously-seeded public image the operator did not migrate into the file is pruned under
-- the cordon guard, not stranded as an unowned orphan). Project-private uploaded images
-- (visibility='private', owner IS NOT NULL — M2.4) stay managed_by='runtime' (the column
-- default), else the first reconcile would prune user uploads.
UPDATE image_catalog SET managed_by = 'config' WHERE visibility = 'public' AND owner IS NULL;
-- build_hosts is intentionally NOT backfilled: it keeps the 'runtime' default because build
-- hosts are imperatively registered (build_hosts.register, ssh hosts), so the first reconcile
-- never prunes them; a config-declared [[build_host]] is adopted in a later phase.
-- affinity already defaults global (owner_project NULL); no allocation regresses.

-- NOTE: NO new system->image column. A reference already exists — the prune guard reuses
-- services/images/retention.py:image_referenced_by_live_system, which resolves "a non-terminal
-- System references this image" via a JSONB-containment probe on systems.provisioning_profile
-- keyed by (provider, name), already ADR-0109 terminal-state-filtered.
