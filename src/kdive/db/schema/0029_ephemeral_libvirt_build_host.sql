-- 0029_ephemeral_libvirt_build_host.sql — admit kind='ephemeral_libvirt' (ADR-0100).
-- Additive, forward-only (ADR-0015). Widen the kind CHECK, add base_image_volume (the
-- operator-staged base build-image volume the build VM overlays), and replace the per-kind
-- field CHECK so each kind constrains its own columns. The build VM lives on the single
-- configured remote-libvirt host (KDIVE_REMOTE_LIBVIRT_*), so an ephemeral row carries no
-- address/ssh_credential_ref — only a base_image_volume.

ALTER TABLE build_hosts DROP CONSTRAINT build_hosts_kind_check;
ALTER TABLE build_hosts ADD CONSTRAINT build_hosts_kind_check
    CHECK (kind IN ('local', 'ssh', 'ephemeral_libvirt'));

ALTER TABLE build_hosts ADD COLUMN base_image_volume text;

ALTER TABLE build_hosts DROP CONSTRAINT build_hosts_ssh_fields_check;
ALTER TABLE build_hosts ADD CONSTRAINT build_hosts_fields_check CHECK (
    (kind = 'local'
        AND address IS NULL AND ssh_credential_ref IS NULL AND base_image_volume IS NULL) OR
    (kind = 'ssh'
        AND address IS NOT NULL AND ssh_credential_ref IS NOT NULL AND base_image_volume IS NULL) OR
    (kind = 'ephemeral_libvirt'
        AND address IS NULL AND ssh_credential_ref IS NULL AND base_image_volume IS NOT NULL)
);
