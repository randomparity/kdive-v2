# ADR 0080 — Remote provisioning: disk-image base-OS profile, domain-XML gdbstub port registry, storage-pool overlay (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0076](0076-remote-libvirt-provider-package.md) (the
  independent `remote_libvirt` package + the portability gate that forbids new core DDL),
  [ADR-0077](0077-qemu-tls-control-transport.md) (the `qemu+tls://` connection every
  provisioning op rides), [ADR-0078](0078-object-store-in-target-install-seam.md) (direct-kernel
  boot retired for remote; the guest-agent seam the provisioned domain must carry),
  [ADR-0079](0079-remote-live-debug-transport.md) (the per-System gdbstub port this plane
  allocates + records), [ADR-0025](0025-provisioning-plane.md) (the Provisioner port contract
  and define/start transactionality), [ADR-0060](0060-per-system-rootfs-overlay.md) (the
  per-System overlay invariant this realizes remotely), [ADR-0024](0024-provisioning-profile-schema.md)
  (the provider-section profile schema this extends).
- **Spec:** [`../specs/m2-remote-libvirt.md`](../specs/m2-remote-libvirt.md) §Decomposition issue 2

## Context

Issue 2 of M2 gives `remote_libvirt` a real Provisioner: a persistent disk-image base-OS System
the kernel-iteration loop reuses (direct-kernel boot is retired for remote, ADR-0078). Four
constraints shape the design:

1. **No shared filesystem.** The worker cannot run `qemu-img` against the remote host's
   storage, so local-libvirt's overlay mechanism (ADR-0060) cannot be transplanted.
2. **The portability gate.** New core DDL (e.g. a `systems.gdbstub_port` column) or a
   `services/` change is the gate firing (ADR-0076). The gdbstub port must be allocated and
   recorded provider-side, and the `Provisioner` port signature (`provision(system_id,
   profile) -> domain_name`) is unchanged (M2 carried invariant 1).
3. **ADR-0079's port contract.** Every running System gets a distinct gdbstub port,
   collision-free across concurrent Systems on one host; the Connect plane (issue 6) reads
   the recorded port. The gdbstub listen address is a security boundary (the ACL is the auth).
4. **The artifact channel does not exist yet** (issue 3). The base image cannot flow through
   the object store in this issue; it must already be present on the remote host.

## Decision

### 1. The profile: a `remote-libvirt` provider section + `BootMethod.DISK_IMAGE`

`ProvisioningProfile.provider` gains a `remote-libvirt` section (`RemoteLibvirtProfile`):

- `base_image_volume` (required) — the name of an **operator-staged** qcow2 volume on the
  remote host carrying the base OS **with qemu-guest-agent enabled, drgn, and matching
  vmlinux/debuginfo** (ADR-0078/0079 image-content obligations). Provisioning verifies the
  volume **exists** at provision time (`CONFIGURATION_ERROR` if absent); the *content*
  obligations are the operator's contract, recorded here and proven by the issue-8 e2e —
  they are not introspectable from a volume lookup.
- `crashkernel` (optional) — the kdump prerequisite token, mirroring the local section;
  consumed by the install plane (issue 5) and the two-phase retrieve (issue 7).
- `destructive_ops` (optional list) — the destructive-op opt-in factor, mirroring the local
  section (deny-by-default, ADR-0028).

`BootMethod` gains `DISK_IMAGE = "disk-image"`. Cross-field validation is strict both ways:
a `remote-libvirt` section requires `boot_method: disk-image`, and `disk-image` requires a
`remote-libvirt` section — local-libvirt and fault-inject keep `direct-kernel`. The
provider-agnostic profile helpers (`capture_method`, `destructive_opt_in`) learn the remote
section; `rootfs_source` / `ssh_credential_ref` already return `None` for it (the remote
provider has no worker-local rootfs and no SSH credential, ADR-0079).

`src/kdive/profiles/` is not a gate-protected core prefix, so this extension is legal under
ADR-0076; the schema mechanism (one section per provider, exactly-one validator) is exactly
what ADR-0024 built it for.

### 2. The gdbstub port: allocated from a configured range, recorded in the domain XML

The domain definition **is the port registry**. Provisioning renders the gdbstub into the
domain XML as QEMU passthrough arguments (`<qemu:commandline><qemu:arg value='-gdb'/>
<qemu:arg value='tcp:ADDR:PORT'/>`), so the record is **atomic with `defineXML`**, freed by
`undefine`, survives worker crashes (it lives on the host, beside the domain it describes),
and is readable over the same TLS connection by the Connect plane (issue 6).

This **refines ADR-0079's recording sketch** ("in System state / `capabilities`"): a
`systems` column is core DDL outside the gate allowlist, and the `capabilities` row is
resource-scoped and insert-if-absent — neither write is atomic with domain creation. The
domain XML is the **registry of record**; the Connect plane reads it over TLS, not the DB.

Allocation: enumerate all **defined** `kdive-`prefixed domains on the host (running or not —
a stopped System still owns its port), parse each one's recorded port, and pick the **lowest
free port** in the operator-configured range. A domain that vanishes between listing and
reading (a concurrent teardown) is **skipped** — its port is being released. If the System's
own domain already records an in-range port (a provision retry), **reuse it** — the port is
stable across retries. An exhausted range is a `PROVISIONING_FAILURE` naming the range and
the count in use.

The range is **reserved for kdive** — an operator obligation recorded alongside the ACL
obligation (ADR-0079): enumeration sees only kdive domains, so a foreign listener inside the
range is invisible to allocation and surfaces only as a bind failure at start. Two workers
provisioning concurrently can also both pick the same lowest-free port; the loser's QEMU
fails to bind at `create()`. Either way the backstop is QEMU's own bind exclusivity — a
collision can never produce two Systems sharing a port (the acceptance invariant) — and a
start failure **advances to the next free candidate port within the same provision attempt**
(bounded by a small fixed retry count, then by the range) rather than re-picking the same
"free" port deterministically forever. The advance is unconditional on the failure's cause
(libvirt does not surface bind-vs-other distinctly enough to sniff messages): if the real
fault is elsewhere, the next attempt fails the same way and the bounded retry stops.

### 3. The overlay: a storage-pool volume with a qcow2 backing store

The per-System writable layer (ADR-0060's invariant) is realized with **libvirt storage-volume
APIs over the same TLS connection**: `virStorageVolCreateXML` in an operator-configured pool,
named `kdive-<system_id>-overlay.qcow2`, format qcow2, with `<backingStore>` pointing at the
base volume's path. Capacity is **inherited from the base volume** (a smaller capacity would
truncate the guest's view of the disk; `disk_gb` remains the scheduler's sizing input, as it
is for local-libvirt overlays). The domain's disk is `<disk type='volume'>` referencing
pool + overlay volume.

Idempotency mirrors ADR-0060: provision creates the overlay only when **absent** (a present
overlay may be held open by a running QEMU; recreating it would corrupt the live disk);
teardown deletes it, an already-absent volume being the achieved post-state. A present
overlay is reused **without** checking its backing store against the profile — provision
retries carry the same profile (the handler re-reads the stored profile), and a profile
*change* goes through `reprovision`, whose teardown deletes the old overlay first.

### 4. Provision/teardown semantics: ADR-0025 transactionality + a guest-agent readiness gate

`provision` is idempotent and transactional in the ADR-0025 sense: deterministic domain name
and uuid (= the System id, so `defineXML` redefines in place on retry), `create()` treating
`VIR_ERR_OPERATION_INVALID` ("already running") as the achieved post-state, and a non-start
failure undefining the just-defined domain and deleting an overlay **this attempt created**
(never a pre-existing one).

After a successful start, provision **waits for the qemu-guest-agent channel** to report
`state='connected'` in the live domain XML, polling over the same connection, bounded by a
timeout. The System's entire purpose is to be driven through the agent seam (install,
in-guest drgn, vmcore upload — ADR-0078/0079); a System whose agent never connects must not
reach `ready`, or every later plane misattributes the fault. The wait is **read-only** (no
agent command is issued — the exec seam is issue 3's; the channel state is libvirt's own
connection tracking). Timeout maps to `PROVISIONING_FAILURE`. The started domain is **left
defined and running** on an agent timeout — unlike a start failure, the domain is the
diagnosable artifact (an operator can inspect its console), and a provision retry converges
(redefine-in-place, already-running, re-poll) without it being torn down first.

`teardown(domain_name)` destroys + undefines idempotently (the local-libvirt error-code
contract, duplicated deliberately — no shared layer, ADR-0076), then deletes the overlay
volume derived from the domain name. `reprovision` = `teardown` + `provision` (ADR-0038's
wipe-and-replace, same as local).

The domain XML carries the kdive metadata tag (same namespace URI as local-libvirt,
duplicated deliberately) so later reaping/discovery can identify kdive-owned domains. It
renders a pty serial console but **no worker-local `<log>` tee** — local-libvirt's console
log path is meaningless on a remote host's filesystem; remote console capture is issue 7's.
TLS-connect failures propagate as `transport_failure` from the ADR-0077 transport, the
spec's documented mapping for every remote plane.

### 5. Configuration: pool and gdbstub knobs are host-level env config

Which storage pool to use and which address/port-range the gdbstub binds are properties of
the **host's** topology, not of one System's profile, so they live beside the URI in the
operator env config (and are advertised into `resources.capabilities` by discovery, per the
spec's "the gdbstub port range are `capabilities` config on the resource"):

- `KDIVE_REMOTE_LIBVIRT_STORAGE_POOL` (default `default`).
- `KDIVE_REMOTE_LIBVIRT_GDB_ADDR` — **required to provision, no default**. The listen
  address is the ACL'd security boundary (ADR-0079: the ACL *is* the auth); defaulting it
  (to `0.0.0.0` or anything else) would silently expose an unauthenticated kernel-control
  port, so the operator must name it explicitly. Absent ⇒ provisioning fails
  `CONFIGURATION_ERROR`; discovery/control (which don't need it) still work.
- `KDIVE_REMOTE_LIBVIRT_GDB_PORT_MIN` / `_MAX` (default `47000`–`47099`).

The runtime stays **buildable without any of this** (ADR-0076): `RemoteLibvirtProvision`
reads the config at op time, exactly as the discovery registrar does.

## Consequences

- The Provisioner port, the `systems.*` handlers, and the DB schema are untouched — the
  gate's allowlist is not extended. The cost is that the port record is invisible to SQL;
  anything that needs it must ask the host (the Connect plane does exactly that, and issue 6
  owns making that read).
- Port allocation requires enumerating defined domains per provision — O(domains on host),
  acceptable at the per-host concurrency caps M2 targets (`concurrent_allocation_cap`,
  default 1–low-tens).
- The agent-readiness gate makes provision slower (up to the poll timeout on a broken image)
  but makes `ready` mean *reachable through the seam every later plane uses* — the issue's
  acceptance criterion ("the guest agent responds") is enforced at the boundary that owns it.
- An agent-timeout leaves a running domain behind by design; the System row goes `failed`
  (the provision handler records the failure and does **not** tear down — verified in
  `jobs/handlers/systems.py`), and the domain is reclaimed by an explicit
  `systems.teardown` or by allocation release (including break-glass and lease expiry),
  both of which call the provider's idempotent `teardown`. Nothing reclaims it
  *automatically* before then: the reconciler's provider reaper composes no remote reaper
  yet (deferred from the discovery foundation, with PCIe enumeration) — recorded as a known
  M2 gap, not silently.
- The operator-staged base volume is a manual prerequisite (documented in the e2e runbook,
  issue 8); issue 3's artifact channel does not replace it (kernels flow through the object
  store; the base OS image does not).

## Alternatives considered

- **Record the gdbstub port in a DB column or in `resources.capabilities`.** A
  `systems.gdbstub_port` column is core DDL outside the ADR-0076 allowlist (the gate firing);
  the `capabilities` row is advisory and insert-if-absent (per the foundation's discovery
  contract) — neither write is atomic with domain creation, so a worker crash between define
  and record strands or double-allocates a port. The domain XML is atomic, crash-consistent,
  and host-local. Rejected.
- **Derive the port from the System id (hash into the range).** No enumeration, but
  collisions are silent until two QEMUs fight over a bind, and the range cannot be safely
  small. Rejected: enumeration is cheap at M2 concurrency and makes exhaustion explicit.
- **Worker-side `qemu-img` / upload the overlay.** No shared filesystem; uploading a
  full base-image copy per System duplicates gigabytes and `virStorageVolUpload` for
  artifacts is rejected by ADR-0078. Rejected.
- **SSH/host-agent to run `qemu-img` on the host.** Reintroduces exactly the host-side
  channel ADR-0078 rejected (a libvirt-only investment, a second secret). Rejected.
- **Skip the guest-agent readiness gate (defer to install, issue 5).** Provision returns
  faster, but `ready` would assert nothing the kernel-iteration loop needs, and an unusable
  image surfaces as a confusing `install_failure` one issue later. Rejected: the acceptance
  criterion names the agent responding.
- **Issue a guest-agent ping (`guest-ping`) instead of reading the channel state.** A real
  agent round-trip, but it is the issue-3 exec seam (and `virDomainQemuAgentCommand` pulls
  in the libvirt-qemu extension API); the channel `state` attribute is libvirt's own
  tracking of the same fact and is read-only. Rejected for this issue; issue 3 may tighten.
- **Put `storage_pool`/gdbstub config in the profile.** A profile is per-System and
  agent-submittable; the pool and the gdbstub listen address are host topology and a
  security boundary respectively — letting a profile steer the gdbstub bind address would
  hand the ACL decision to the caller. Rejected.
