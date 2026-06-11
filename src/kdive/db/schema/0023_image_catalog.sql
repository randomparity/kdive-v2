-- 0023_image_catalog.sql — M2.4 image & rootfs lifecycle (ADR-0092, ADR-0093).
-- The single M2.4 migration: the full public + private image_catalog schema. The DB-backed
-- catalog replaces the read-only YAML rootfs catalog as the single source of truth. Mirrors
-- ImageCatalogEntry + ImageVisibility/ImageState in domain/models.py; named CHECKs encode the
-- closed value sets (tied to the enums by tests/db/test_migrate.py). Columns the later M2.4
-- issues (#285/#286/#287) consume — owner, expires_at, pending_since, state — are authored here
-- so no later issue adds a second migration (the single-migration-owner rule).
CREATE TABLE image_catalog (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider      text        NOT NULL,
    name          text        NOT NULL,
    arch          text        NOT NULL,
    format        text        NOT NULL,
    root_device   text        NOT NULL,
    -- object-store key of the qcow2; NULL for a `defined` row (metadata seeded, no image yet).
    object_key    text,
    -- qcow2 content digest (the image identity); NULL until the image is built.
    digest        text,
    capabilities  text[]      NOT NULL DEFAULT '{}',
    provenance    jsonb       NOT NULL DEFAULT '{}',
    visibility    text        NOT NULL CONSTRAINT image_visibility_check
                              CHECK (visibility IN ('public', 'private')),
    -- owning project iff private; required iff private (the two CHECKs below tie them).
    owner         text,
    expires_at    timestamptz,
    state         text        NOT NULL DEFAULT 'defined' CONSTRAINT image_state_check
                              CHECK (state IN ('defined', 'pending', 'registered')),
    -- timestamp the publishing row was (re)armed; backs the publish-deadline grace window.
    pending_since timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    -- A `defined` baseline has no object; a `pending`/`registered` row does.
    CONSTRAINT image_object_present CHECK ((state = 'defined') = (object_key IS NULL)),
    CONSTRAINT image_private_owner CHECK ((visibility = 'private') = (owner IS NOT NULL)),
    CONSTRAINT image_private_expiry CHECK ((visibility = 'private') = (expires_at IS NOT NULL))
);
CREATE TRIGGER image_catalog_set_updated_at BEFORE UPDATE ON image_catalog
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();
-- One registered public image per identity. `pending` rows are excluded so a crashed publish's
-- leftover `pending` row never wedges a re-publish of the same identity.
CREATE UNIQUE INDEX image_catalog_one_public ON image_catalog (provider, name, arch)
    WHERE state = 'registered' AND visibility = 'public';
-- A project's private image name resolves to exactly one registered image.
CREATE UNIQUE INDEX image_catalog_one_private ON image_catalog (owner, provider, name)
    WHERE state = 'registered' AND visibility = 'private';
-- At most one seeded `defined` baseline per public identity (seed idempotency at the DB level).
CREATE UNIQUE INDEX image_catalog_one_defined ON image_catalog (provider, name, arch)
    WHERE state = 'defined' AND visibility = 'public';
