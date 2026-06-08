CREATE TABLE provider_components (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider text NOT NULL,
    component_kind text NOT NULL,
    source jsonb NOT NULL,
    artifact_id uuid,
    visibility text NOT NULL CONSTRAINT provider_components_visibility_check
        CHECK (visibility IN ('public', 'project', 'host-policy')),
    project text,
    principal text NOT NULL,
    sha256 text CONSTRAINT provider_components_sha256_check
        CHECK (sha256 IS NULL OR sha256 ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT provider_components_project_visibility_check
        CHECK ((visibility = 'project' AND project IS NOT NULL) OR visibility <> 'project')
);
CREATE TRIGGER provider_components_set_updated_at BEFORE UPDATE ON provider_components
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();
CREATE INDEX provider_components_provider_kind_idx
    ON provider_components (provider, component_kind);
CREATE INDEX provider_components_project_idx ON provider_components (project);

CREATE TABLE component_uploads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant text NOT NULL,
    provider text NOT NULL,
    component_kind text NOT NULL,
    artifact_id uuid,
    sha256 text NOT NULL CONSTRAINT component_uploads_sha256_check
        CHECK (sha256 IS NULL OR sha256 ~ '^sha256:[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CONSTRAINT component_uploads_size_positive_check
        CHECK (size_bytes > 0),
    visibility text NOT NULL CONSTRAINT component_uploads_visibility_check
        CHECK (visibility IN ('public', 'project')),
    project text NOT NULL,
    principal text NOT NULL,
    state text NOT NULL CONSTRAINT component_uploads_state_check
        CHECK (state IN ('pending', 'finalized', 'failed')),
    deadline timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER component_uploads_set_updated_at BEFORE UPDATE ON component_uploads
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();
CREATE INDEX component_uploads_project_state_idx ON component_uploads (project, state);
