-- 0027_build_hosts.sql — Remote build-host inventory + capacity leases (ADR-0099).
-- Additive, forward-only (ADR-0015). build_hosts is the selection seam; build_host_leases
-- is the per-in-flight-build capacity record (rows counted under the BUILD_HOST advisory
-- lock — this codebase models capacity by counting rows, not an integer column).

CREATE TABLE build_hosts (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name               text UNIQUE NOT NULL,
    kind               text NOT NULL CONSTRAINT build_hosts_kind_check
                       CHECK (kind IN ('local', 'ssh')),
    address            text,
    ssh_credential_ref text,
    workspace_root     text NOT NULL,
    max_concurrent     integer NOT NULL CONSTRAINT build_hosts_capacity_check
                       CHECK (max_concurrent > 0),
    enabled            boolean NOT NULL DEFAULT true,
    state              text NOT NULL DEFAULT 'ready' CONSTRAINT build_hosts_state_check
                       CHECK (state IN ('ready', 'unreachable')),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT build_hosts_ssh_fields_check CHECK (
        (kind = 'ssh'  AND address IS NOT NULL AND ssh_credential_ref IS NOT NULL) OR
        (kind = 'local' AND address IS NULL AND ssh_credential_ref IS NULL)
    )
);
CREATE TRIGGER build_hosts_set_updated_at BEFORE UPDATE ON build_hosts
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE build_host_leases (
    run_id        uuid PRIMARY KEY,
    build_host_id uuid NOT NULL REFERENCES build_hosts(id) ON DELETE RESTRICT,
    acquired_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX build_host_leases_by_host ON build_host_leases (build_host_id);

-- Seed the default local fallback. Fixed UUID so the row is identifiable/protected in code.
-- max_concurrent is informational for the local row (local builds acquire no lease).
INSERT INTO build_hosts (id, name, kind, workspace_root, max_concurrent)
VALUES ('00000000-0000-0000-0000-0000000000c0', 'worker-local', 'local',
        '/var/lib/kdive/build', 1000);
