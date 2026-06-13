# ADR 0100 — Ephemeral remote-libvirt build VM (`kind='ephemeral_libvirt'`)

- **Status:** Proposed
- **Date:** 2026-06-13
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0099](0099-remote-build-host-targets.md)
  (the `BuildTransport` seam, `build_hosts` inventory, selection + fail-fast capacity, and
  presigned-PUT artifact upload this extends with a third `kind`),
  [ADR-0080](0080-remote-provisioning-disk-image-profile.md) (the remote `qemu+tls://`
  define/start + qcow2-overlay + agent-readiness lifecycle this reuses for the build VM),
  [ADR-0078](0078-object-store-in-target-install-seam.md) (the qemu-guest-agent in-target
  exec channel the new transport runs over),
  [ADR-0081](0081-remote-build-kernel-bundle.md) (the `RemoteLibvirtBuild` builder + bundle
  pipeline the transport-bound build reuses unchanged), and
  [ADR-0091](0091-doctor-diagnostics-model.md) (the ephemeral-probe-guest reaper-by-marker
  pattern the build-VM reaper mirrors).
- **Spec:** [`../superpowers/specs/2026-06-13-ephemeral-libvirt-build-vm.md`](../superpowers/specs/2026-06-13-ephemeral-libvirt-build-vm.md)
- **Issue:** [#355](https://github.com/randomparity/kdive/issues/355)
- **Milestone:** remote build-host targets

## Context

ADR-0099 designed three build-host targets and shipped two: `kind='local'` (worker-local
warm-tree `make`) and `kind='ssh'` (a dedicated admin-registered SSH host). It explicitly
deferred target 3 — an **ephemeral VM provisioned on demand on the operator's remote-libvirt
host**, the build dispatched over the in-guest exec/presigned channel, the VM torn down
afterward — and designed the seam and schema to admit it: the `build_hosts.kind` CHECK was
written to take a third value, and the `BuildTransport` port abstracts host-side primitives
so a new realization slots under the existing `BuildHostOrchestrator` / `RemoteLibvirtBuild`
without touching `BuildOutput`, the `Builder` port, or the `runs` ledger.

The leverage points already exist:

- **The build is transport-agnostic.** `RemoteLibvirtBuild.over_transport` binds every slow
  step (git checkout, `olddefconfig`, `.config` read, `make`, `modules_install`, build-id,
  bundle, `vmlinux`) to an injected `BuildTransport`, and publishes each artifact via a
  presigned PUT whose checksum is computed host-side. Target 2 (SSH) is exactly this
  pattern; target 3 reuses it with a different transport.
- **A remote VM lifecycle exists.** `RemoteLibvirtProvisioning` (ADR-0080) defines+starts a
  `qemu+tls://` domain with a qcow2 overlay over an operator-staged base volume and waits for
  its guest agent — over the same mutual-TLS connection a build VM would use.
- **An in-guest exec channel exists.** `GuestAgentExec` (ADR-0078) runs worker-composed,
  allowlisted commands in-guest via the two-phase `guest-exec`/`guest-exec-status` protocol,
  capturing stdout/stderr; `InTargetArtifactChannel` registers a minted presigned URL for
  redaction before the exec.
- **A reap-by-marker pattern exists.** The egress-probe guest (ADR-0091) and the host_dump
  volume reaper (ADR-0094) both reap orphaned remote infrastructure by a deterministic
  name marker plus a live-holder guard; the reconciler already reclaims orphaned
  `build_host_leases` by owning-BUILD-job liveness (ADR-0099).

What target 3 needs that does not exist: (1) a `BuildTransport` over the guest-agent exec
channel; (2) a provision→build→teardown lifecycle for a dedicated build VM (not a debug
target); (3) reconciler reaping of a leaked build VM; (4) a registration/selection model for
an `ephemeral_libvirt` build host.

Two design forks are load-bearing. **First, the build VM's domain identity.** It can be a
plain provider-managed libvirt domain reaped by marker, or a first-class kdive `System` on
the Allocation/Run/state-machine spine. **Second, working directories.** qemu-guest-agent's
`guest-exec` has no working-directory argument, yet two build steps (`merge_config.sh -m`,
`git apply`) are cwd-dependent, and ADR-0078's exec channel deliberately avoids delegating
to a general in-guest shell.

## Decision

1. **Add `kind='ephemeral_libvirt'` to the `build_hosts` inventory; the row references the
   single configured remote-libvirt host implicitly and names an operator-staged base build
   image.** Migration 0029 (additive/forward-only, ADR-0015) widens the `build_hosts_kind`
   CHECK to admit `'ephemeral_libvirt'`, adds a nullable `base_image_volume` column, and
   replaces the per-kind field CHECK so each kind constrains its own columns:
   `local` → all of `address`/`ssh_credential_ref`/`base_image_volume` NULL; `ssh` →
   `address`+`ssh_credential_ref` NOT NULL, `base_image_volume` NULL; `ephemeral_libvirt` →
   `address`+`ssh_credential_ref` NULL, `base_image_volume` NOT NULL. The build VM is
   provisioned on the deployment's single `KDIVE_REMOTE_LIBVIRT_*`-configured host (the same
   one the remote-libvirt provider uses), so the row carries no per-row host address — "it
   lives on an already-registered remote-libvirt host." `workspace_root` is the **in-guest**
   path the build runs under; `max_concurrent` caps concurrent build VMs and is enforced by
   the **existing** lease seam (`kind != 'local'` already acquires a `build_host_leases` row
   under the `BUILD_HOST` advisory lock). No new capacity machinery, no new `ErrorCategory`.

2. **`build_hosts.register` admits both remote kinds via a `kind` argument; per-kind required
   fields are validated.** `register` gains an optional `kind` (default `'ssh'`,
   back-compatible) ∈ {`ssh`, `ephemeral_libvirt`}. `ssh` requires `address` +
   `ssh_credential_ref` (unchanged); `ephemeral_libvirt` requires `base_image_volume` and
   rejects `address`/`ssh_credential_ref`. It remains `PLATFORM_ADMIN` + audited; `list` /
   `disable` / `remove` and the protected `worker-local` seed are unchanged. An
   `ephemeral_libvirt` host carries no SSH credential, so the secret-ref validation applies
   only to the `ssh` kind.

3. **The build VM is a plain provider-managed libvirt domain reaped by marker — NOT a kdive
   System.** The BUILD job handler, for an `ephemeral_libvirt` host, provisions a domain
   named `kdive-build-<run_id>` on the remote-libvirt host (qcow2 overlay over
   `base_image_volume`, guest-agent channel, no gdbstub — a builder is not a debug target),
   waits for its guest agent, runs the build over a `GuestExecBuildTransport` bound to the
   domain handle, and tears the domain + overlay down in a `finally`. The domain name is the
   reaper-visible marker (it embeds `run_id`); no new DB table is needed because the
   `build_host_leases` row and the owning BUILD job already track the run. A System would
   force every build onto the Allocation/quota/state-machine spine the builder does not use
   and would couple build teardown to System reaping — dead weight for a throwaway builder.

4. **`GuestExecBuildTransport` realizes `BuildTransport` over `GuestAgentExec`, composing one
   fixed in-guest shell hop per command — the same posture as the sibling SSH transport.**
   `guest-exec` has no cwd argument, so the transport composes
   `/bin/sh -c "cd <quoted cwd> && exec <shlex.join(argv)>"` and runs it through
   `GuestAgentExec` with the allowlist `{'/bin/sh'}` and the call's `timeout_s`. This is
   exactly what `SshBuildTransport` already does (ssh runs the command through the remote
   login shell, `cd <cwd> && <cmd>`): the worker composes fixed, shell-quoted argv, so there
   is no injection surface. The "no general in-guest shell" rule in ADR-0078 governs the
   **debug-target** install/capture channel, where the guest runs a caller's kernel and the
   allowlist bounds blast radius; the build VM is a different trust context — ephemeral,
   dedicated to one build, torn down after — so matching the SSH target's shell hop is the
   consistent choice and avoids an operator image-build requirement. File reads/writes,
   `clone`, `upload_file`, and `cleanup` reuse the SSH transport's mechanics (base64 framing,
   `git init`+shallow-fetch+`checkout FETCH_HEAD`, `curl` presigned PUT) via a shared base.

5. **Extract a shared `ShellBuildTransport` base; both SSH and guest-exec subclass it.** The
   two remote transports share their entire `BuildTransport` surface and differ only in the
   primitive that runs one argv on the host (`_run_remote`). A `ShellBuildTransport` base
   implements `run`/`read_text`/`read_bytes`/`write_bytes`/`clone`/`upload_file`/`cleanup` in
   terms of an abstract `_run_remote(argv, cwd, timeout_s) -> CommandResult`; `SshBuildTransport`
   provides the ssh primitive (keeping its stdin-streamed `write_bytes` and materialized
   identity), and `GuestExecBuildTransport` provides the guest-agent primitive. The existing
   SSH unit tests pin the refactor as behavior-preserving.

6. **The reconciler reaps a leaked build VM by domain marker + owning-BUILD-job liveness,
   never by age.** A new provider port `BuildVmReaper` lists `kdive-build-*` domains (and
   deletes one by name, with its overlay) — the narrow libvirt I/O seam, mirroring
   `RemoteLibvirtDumpVolumeReaper`. The reconciler extracts `run_id` from each domain name and
   reaps only when no BUILD job for that `run_id` is live (queued/running) — the same
   job-liveness guard `reclaim_orphan_build_host_leases` uses, so a build running up to
   `MAKE_TIMEOUT_S` (2h) is never torn down mid-build. The two reapers are complementary: lease
   reclaim frees the capacity *slot*, VM reaping frees the *domain*; both are job-liveness keyed.

7. **Secrets and redaction follow the established remote contract.** A private git remote's
   credential resolves at the worker boundary, registers into the redaction registry, and only
   `(present, source-ref)` persists. The presigned PUT URL is a bearer capability: the
   transport registers it in the redaction registry **before** the in-guest `curl` and redacts
   all captured output, so a transcript or error detail never carries a live URL. The TLS
   client cert is consumed by the libvirt transport layer and never reaches the exec seam.

8. **Scope.** This ADR's PR implements migration 0029, the `BuildHost.base_image_volume`
   field, the `register`/`resolve_and_admit` `ephemeral_libvirt` admission, the
   `ShellBuildTransport` extraction + `GuestExecBuildTransport`, the build-VM
   provision/teardown lifecycle + build-domain XML, the BUILD-handler `ephemeral_libvirt`
   branch, and the reconciler build-VM reaper. The real provision/exec/teardown path is
   exercised only under the `live_vm` gate (unchanged gate, not widened). Build-VM autoscaling,
   image caching, and a FIFO capacity queue stay out (ADR-0099 alternatives).

## Consequences

- A third build target ships behind the same selection seam: with an `ephemeral_libvirt` host
  registered and a git-source profile naming it, a server-lane build provisions a throwaway VM
  on the remote-libvirt host, builds in-guest, publishes via presigned PUT, and tears the VM
  down — no warm tree or toolchain on the worker, no dedicated SSH host.
- One migration (0029: widen kind CHECK, replace field CHECK, add `base_image_volume`), one new
  transport + a base-class extraction of the existing SSH transport, one provider lifecycle
  module + build-domain XML, one BUILD-handler branch, and one reconciler reaper + provider
  port. No change to `BuildOutput`, the `Builder` port, the `runs` ledger, the capacity/lease
  model, or the error taxonomy.
- The `ephemeral_libvirt` row depends on the deployment's single remote-libvirt host config; a
  deployment without `KDIVE_REMOTE_LIBVIRT_*` configured cannot use this kind (selection still
  resolves the row, but provisioning fails `CONFIGURATION_ERROR` exactly as the remote-libvirt
  provider does today). Multiple remote-libvirt hosts are out of scope (one configured host,
  consistent with the rest of the remote provider).
- The build VM runs `/bin/sh -c` for each step. This is the same shell posture as the SSH
  target and is bounded by: an operator-staged image, an ephemeral single-build VM,
  worker-composed shell-quoted fixed argv, the component-root allowlist gating config/patch,
  and the `make` timeout. It is a deliberate divergence from the debug-target channel's
  no-shell rule, recorded here.
- A build that crashes the worker between provision and the teardown `finally` leaks a domain;
  the reconciler reaper is the backstop, keyed on BUILD-job liveness so it never races a live
  build. Until a reaper sweep runs, a leaked VM holds host resources and its lease holds a
  capacity slot (the lease reclaim is the existing backstop for the slot).

## Alternatives considered

- **Model the build VM as a first-class `System`.** Reuse the Allocation/Run/state-machine
  spine and System reaping. Rejected: a builder is not a debug target; this forces build
  admission through System quota and couples build teardown to System reaping — machinery the
  throwaway builder does not need. The bare-domain + marker-reaper path is lighter and reuses
  the egress-probe/dump-volume precedent.
- **Allowlisted in-guest build-runner helper (the #204 pattern).** Stage a `kdive-build-exec`
  helper into the base image; allowlist only it; it chdirs and execs. Preserves "one allowlisted
  program," but adds an operator image-build requirement and the helper still execs arbitrary
  build tools, so the allowlist constrains little for a build VM. Rejected in favor of matching
  the SSH target's shell hop (no image burden, identical security posture to the sibling target).
- **Make every build step cwd-independent (absolute paths only).** Avoid the shell hop by
  rewriting `merge_config.sh`/`git apply` to absolute paths. Rejected: `git apply` is reachable
  via `git -C`, but `merge_config.sh -m` reads/writes relative to `$PWD` and is fragile to drive
  cwd-free; the rewrite would diverge the transport-seam call sites from the SSH path for a
  marginal gain.
- **A new `ErrorCategory` for build-VM provisioning failure.** Rejected: provisioning maps to
  the existing `PROVISIONING_FAILURE`, transport faults to `TRANSPORT_FAILURE`/`BUILD_FAILURE`,
  and capacity to `CAPACITY_EXHAUSTED` (ADR-0099). No genuine new failure mode.
- **A reaper DB table for build VMs (mirroring `egress_probe_guests`).** Rejected: the
  `build_host_leases` row and the BUILD job already track the run, and the domain name embeds
  `run_id`; a separate heartbeat/TTL table is redundant when job liveness is the reclaim trigger.
- **Per-row remote-libvirt host address on the `ephemeral_libvirt` row.** Rejected: the
  remote-libvirt provider is configured for one host via `KDIVE_REMOTE_LIBVIRT_*`; overloading
  `address` to carry a libvirt URI would muddy the column semantics and imply multi-host support
  that the provider does not have.
