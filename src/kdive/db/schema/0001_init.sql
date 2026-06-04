-- 0001_init.sql — M0 walking-skeleton schema (ADR-0003, ADR-0005, ADR-0015).
-- Mirrors src/kdive/domain/{models,state,errors}.py. text + named CHECK encode the
-- closed value sets; updated_at is trigger-maintained (changed-at semantics).

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TABLE resources (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind         text NOT NULL CONSTRAINT resources_kind_check
                     CHECK (kind IN ('local-libvirt')),
    capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
    pool         text NOT NULL,
    cost_class   text NOT NULL,
    status       text NOT NULL CONSTRAINT resources_status_check
                     CHECK (status IN ('available', 'degraded', 'offline')),
    host_uri     text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER resources_set_updated_at BEFORE UPDATE ON resources
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE allocations (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id      uuid NOT NULL REFERENCES resources (id),
    state            text NOT NULL CONSTRAINT allocations_state_check
                         CHECK (state IN ('requested', 'granted', 'active',
                                          'releasing', 'released', 'failed')),
    lease_expiry     timestamptz,
    capability_scope jsonb NOT NULL DEFAULT '{}'::jsonb,
    principal        text NOT NULL,
    agent_session    text,
    project          text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER allocations_set_updated_at BEFORE UPDATE ON allocations
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE systems (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    allocation_id        uuid NOT NULL REFERENCES allocations (id),
    state                text NOT NULL CONSTRAINT systems_state_check
                             CHECK (state IN ('defined', 'provisioning', 'ready',
                                              'crashed', 'torn_down', 'failed')),
    provisioning_profile jsonb NOT NULL,
    target_fingerprint   text,
    domain_name          text,
    principal            text NOT NULL,
    agent_session        text,
    project              text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER systems_set_updated_at BEFORE UPDATE ON systems
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE investigations (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title         text NOT NULL,
    external_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    state         text NOT NULL CONSTRAINT investigations_state_check
                      CHECK (state IN ('open', 'active', 'closed', 'abandoned')),
    last_run_at   timestamptz,
    principal     text NOT NULL,
    agent_session text,
    project       text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER investigations_set_updated_at BEFORE UPDATE ON investigations
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE runs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    investigation_id uuid NOT NULL REFERENCES investigations (id),
    system_id        uuid NOT NULL REFERENCES systems (id),
    state            text NOT NULL CONSTRAINT runs_state_check
                         CHECK (state IN ('created', 'running', 'succeeded',
                                          'failed', 'canceled')),
    build_profile    jsonb NOT NULL,
    kernel_ref       text,
    debuginfo_ref    text,
    failure_category text CONSTRAINT runs_failure_category_check
                         CHECK (failure_category IN (
                             'configuration_error', 'missing_dependency',
                             'build_failure', 'boot_timeout', 'readiness_failure',
                             'debug_attach_failure', 'infrastructure_failure',
                             'stale_handle', 'transport_conflict', 'not_implemented',
                             'allocation_denied', 'lease_expired',
                             'provisioning_failure', 'install_failure',
                             'transport_failure', 'control_failure',
                             'authorization_denied')),
    principal        text NOT NULL,
    agent_session    text,
    project          text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER runs_set_updated_at BEFORE UPDATE ON runs
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE run_steps (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id     uuid NOT NULL REFERENCES runs (id),
    step       text NOT NULL,
    state      text NOT NULL,
    result     jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT run_steps_run_id_step_key UNIQUE (run_id, step)
);
CREATE TRIGGER run_steps_set_updated_at BEFORE UPDATE ON run_steps
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE debug_sessions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              uuid NOT NULL REFERENCES runs (id),
    state               text NOT NULL CONSTRAINT debug_sessions_state_check
                            CHECK (state IN ('attach', 'live', 'detached')),
    transport           text NOT NULL,
    transport_handle    text,
    worker_heartbeat_at timestamptz,
    principal           text NOT NULL,
    agent_session       text,
    project             text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER debug_sessions_set_updated_at BEFORE UPDATE ON debug_sessions
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             text NOT NULL CONSTRAINT jobs_kind_check
                         CHECK (kind IN ('provision', 'teardown', 'build', 'install',
                                         'boot', 'force_crash', 'power',
                                         'capture_vmcore')),
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    state            text NOT NULL CONSTRAINT jobs_state_check
                         CHECK (state IN ('queued', 'running', 'succeeded',
                                          'failed', 'canceled')),
    attempt          integer NOT NULL DEFAULT 0,
    max_attempts     integer NOT NULL,
    worker_id        text,
    lease_expires_at timestamptz,
    heartbeat_at     timestamptz,
    result_ref       text,
    error_category   text CONSTRAINT jobs_error_category_check
                         CHECK (error_category IN (
                             'configuration_error', 'missing_dependency',
                             'build_failure', 'boot_timeout', 'readiness_failure',
                             'debug_attach_failure', 'infrastructure_failure',
                             'stale_handle', 'transport_conflict', 'not_implemented',
                             'allocation_denied', 'lease_expired',
                             'provisioning_failure', 'install_failure',
                             'transport_failure', 'control_failure',
                             'authorization_denied')),
    authorizing      jsonb NOT NULL,
    dedup_key        text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_dedup_key_key UNIQUE (dedup_key)
);
CREATE TRIGGER jobs_set_updated_at BEFORE UPDATE ON jobs
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE artifacts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_kind      text NOT NULL,
    owner_id        uuid NOT NULL,
    object_key      text NOT NULL,
    etag            text NOT NULL,
    sensitivity     text NOT NULL CONSTRAINT artifacts_sensitivity_check
                        CHECK (sensitivity IN ('sensitive', 'redacted')),
    retention_class text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER artifacts_set_updated_at BEFORE UPDATE ON artifacts
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE audit_log (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ts            timestamptz NOT NULL DEFAULT now(),
    principal     text NOT NULL,
    agent_session text,
    project       text NOT NULL,
    tool          text NOT NULL,
    object_kind   text NOT NULL,
    object_id     uuid NOT NULL,
    transition    text NOT NULL,
    args_digest   text NOT NULL
);
