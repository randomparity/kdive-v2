-- Build-config catalog (ADR-0096): one row per seeded kernel-config fragment.
-- object_key points at a fixed reserved object-store key (system/build-configs/<name>/...),
-- NOT a project-scoped artifacts row. sha256 binds the row to the published bytes.
CREATE TABLE build_config_catalog (
    name        text PRIMARY KEY,
    object_key  text NOT NULL,
    sha256      text NOT NULL,
    description text NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
