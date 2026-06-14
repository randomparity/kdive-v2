# Ephemeral remote-libvirt build VM (target 3) — implementation spec

- **Date:** 2026-06-13
- **Issue:** [#355](https://github.com/randomparity/kdive/issues/355)
- **ADR:** [ADR-0100](../../adr/0100-ephemeral-libvirt-build-vm.md)
- **Builds on:** [ADR-0099 design](2026-06-13-remote-build-host-design.md) §4 (target 3)
- **Status:** Approved (design)

## 1. Problem

ADR-0099 shipped build targets 1 (`local`) and 2 (`ssh`) and deferred target 3: an
**ephemeral VM provisioned on demand on the operator's remote-libvirt host**, the server-lane
build dispatched over the in-guest guest-agent exec channel, the VM torn down afterward. The
`build_hosts` schema and the `BuildTransport` port were designed to admit it. This spec
implements it as `kind='ephemeral_libvirt'`, reusing the ADR-0099 selection/capacity seam and
the ADR-0080 remote-libvirt lifecycle, and adding the four pieces #355 names: build-VM
lifecycle, a guest-exec `BuildTransport`, reconciler reaping, and the registration/selection
model.

## 2. Goal

A server-lane Run whose profile names an `ephemeral_libvirt` build host and a **git**
`kernel_source_ref` provisions a throwaway VM on the configured remote-libvirt host, runs the
build in-guest, publishes `kernel_ref`/`debuginfo_ref`/`build_id` into the `runs` ledger via
presigned PUT **exactly** as the SSH target does, and tears the VM down. The local and SSH
targets are unchanged. `BuildOutput`, the `Builder` port, the `runs` ledger, the capacity/lease
model, and the error taxonomy are unchanged.

## 3. Non-goals

- Multiple remote-libvirt hosts. The provider is configured for one host
  (`KDIVE_REMOTE_LIBVIRT_*`); the `ephemeral_libvirt` row references it implicitly.
- Build-VM autoscaling, image caching/ccache, a FIFO capacity queue (ADR-0099 alternatives).
- Building the base build image. It is operator-staged (toolchain + git + qemu-guest-agent +
  `/bin/sh` + `curl` + `base64`), exactly as the remote-libvirt base OS image is.
- Widening the `live_vm` gate. The real provision/exec/teardown path stays gated.

## 4. Architecture

### 4.1 The seam stays where ADR-0099 put it

The build is already transport-agnostic: `RemoteLibvirtBuild.over_transport(transport, …)`
binds every slow step to an injected `BuildTransport` and publishes via presigned PUT
(host-side checksum). Target 3 is **a new `BuildTransport` realization plus the provision /
teardown lifecycle around it** — not a new orchestrator or builder.

### 4.2 `ShellBuildTransport` base + `GuestExecBuildTransport`

The SSH and guest-exec transports share their entire `BuildTransport` surface and differ only
in the primitive that runs one argv on the host. Extract a base:

```python
class ShellBuildTransport:  # providers/build_host/shell_transport.py
    def _run_remote(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult: ...  # abstract

    # implemented once in the base, in terms of _run_remote:
    def run(self, argv, *, cwd, timeout_s) -> CommandResult        # delegates to _run_remote
    def read_text(self, path) -> str                                # base64 -w0, utf-8 decode
    def read_bytes(self, path) -> bytes                             # base64 -w0, size-capped
    def clone(self, remote, ref, dest) -> None                      # git init + fetch --depth 1 + checkout FETCH_HEAD
    def upload_file(self, path, presigned) -> str                   # curl -fsS -X PUT --upload-file, parse ETag
    def cleanup(self, path) -> None                                 # rm -rf

    def write_bytes(self, path, data) -> None: ...                  # ABSTRACT — see note below
```

`write_bytes` is **not** a generic-argv operation and is left abstract on the base: each
subclass implements it with the framing its primitive supports (the generic `_run_remote`
runs one argv and cannot express a stdin stream or a shell pipeline/redirection). `run`,
`read_text`, `read_bytes`, `clone`, `upload_file`, and `cleanup` are the only methods the base
implements in terms of `_run_remote`.

- `SshBuildTransport` subclasses it, providing `_run_remote` (the `ssh -i … host "cd … && …"`
  primitive) and implementing `write_bytes` via its stdin-streamed base64 (`base64 -d > path`
  with `input=encoded`, any size — its current behavior, preserved).
  Its `materialized_ssh_identity` lifecycle and `from_host` are unchanged. The shared pure
  helpers (`_validate_git_arg`, `_validate_ssh_destination` stays ssh-local, `_extract_etag_from_headers`,
  the read-size cap, `redacted_tail` usage) move to the base or a shared module. **The existing
  SSH unit tests must stay green — the refactor is behavior-preserving.**
- `GuestExecBuildTransport` subclasses it, providing `_run_remote` over the guest agent:

```python
def _run_remote(self, argv, *, cwd, timeout_s) -> CommandResult:
    remote = f"cd {shlex.quote(cwd)} && exec {shlex.join(argv)}"
    agent = GuestAgentExec(agent_command=self._agent_command,
                           allowed_programs=frozenset({"/bin/sh"}),
                           timeout_s=timeout_s, poll_s=self._poll_s, ...injected clocks)
    result = agent.run(self._domain, ["/bin/sh", "-c", remote])
    return CommandResult(result.exit_status, result.stdout.decode(...), result.stderr.decode(...))
```

`GuestAgentExec`'s timeout is fixed at construction, and `BuildTransport.run` carries a
per-call `timeout_s` (a `make` may run 2h while a `.config` read is seconds), so the transport
constructs a `GuestAgentExec` per `_run_remote` with that call's `timeout_s`. `GuestAgentExec`
is reused **unchanged** (it already enforces the `argv[0]` allowlist, the two-phase
exec/status protocol, and signal→`128+sig` exit mapping); the allowlist is `{'/bin/sh'}`.

`GuestExecBuildTransport.write_bytes` composes its **own** in-guest command —
`/bin/sh -c "printf %s '<b64>' | base64 -d > '<quoted path>'"` — run directly through the
agent (not via `_run_remote`'s `exec <join(argv)>` form, which cannot express the pipe).
`<b64>` is base64 (alphanumeric + `+/=`, no shell metacharacters) and the path is `shlex`-
quoted. The only writes are the config fragment and the patch bytes, both small (KB), well
under `ARG_MAX`.

`AgentExecResult.stdout/stderr` are bytes; decode UTF-8 with `errors="replace"` into
`CommandResult` (the build reads return codes and small text configs; non-UTF-8 stderr is
captured for redaction, not parsed).

### 4.3 Build-VM lifecycle

New `providers/remote_libvirt/lifecycle/build_vm.py` — `EphemeralBuildVm`, reusing the
ADR-0080 collaborators (`remote_connection`, `lookup_pool`, `ensure_overlay`, `delete_volume`,
`wait_for_agent`) and a **new build-domain XML renderer** (`render_build_domain_xml`): the
guest-agent virtio-serial channel, the overlay disk, the configured network (for git + S3
egress), generous vCPU/RAM for compiling, and **no gdbstub** (a builder is not a debug target).

```python
@contextmanager
def session(host: BuildHost, secret_registry, *, run_id) -> Iterator[GuestExecBuildTransport]:
    # provision: lookup pool, ensure overlay over host.base_image_volume keyed by run_id,
    #            define+start kdive-build-<run_id>, wait_for_agent
    # yield a GuestExecBuildTransport bound to the domain handle + a live agent_command
    # finally: teardown (destroy + undefine + delete overlay), best-effort; reaper backstops
```

The domain name `kdive-build-<run_id>` (a fixed prefix + the run UUID) is the reaper marker.
Provisioning idempotency follows ADR-0080: a deterministic name+overlay redefines/reuses on
retry. The blocking libvirt calls are offloaded (`asyncio.to_thread`) in the handler;
`session` itself is synchronous like the SSH `from_host`.

**Shared-namespace invariants (build VM coexists with System guests on the same host/pool).**
The build domain MUST record **no gdbstub** `<qemu:commandline>` port: `used_gdb_ports`
enumerates every `kdive-`-prefixed domain but skips those whose `recorded_gdb_port` is `None`
(gdb.py), so a gdbstub-less build domain is inert for System port allocation — a regression
test pins this (a `kdive-build-*` domain does not appear in `used_gdb_ports`). The build
overlay name MUST be disjoint from a System overlay name (a distinct `kdive-build-<run_id>`
volume name, not the `overlay_volume_name(system_id)` scheme), so neither System teardown nor
the host_dump volume reaper (which matches `kdive-host-dump-<uuid>.kdump`) can match it and the
build-VM reaper cannot match a System overlay. Per-run-id UUIDs are unique, but the *name
formats* must not collide.

**Retry workspace.** The build runs in the orchestrator's per-run subdir
(`workspace_root/<run_id>`, orchestration.py); on the happy path the VM is fresh, so the
workspace is clean. A BUILD-job retry on the idempotently-reused VM inherits the orchestrator's
existing per-run-dir reuse semantics — identical to the SSH target, no new behavior introduced
here.

### 4.4 BUILD-handler branch

`jobs/handlers/runs.py:_run_build` dispatches on `host.kind`:

```
host.kind == 'local'            -> runtime builder, no transport (today)
host.kind == 'ssh'              -> SshBuildTransport.from_host (today)
host.kind == 'ephemeral_libvirt'-> EphemeralBuildVm.session(host, …, run_id)  (NEW)
```

The ephemeral branch mirrors the ssh branch: open the session context manager (provision →
yield transport → teardown in `finally`), bind the transport-capable `RemoteLibvirtBuild` via
`build_over_transport(builder, transport, host_workspace_root=host.workspace_root, git_remote,
git_ref, secret_registry)`, and `asyncio.to_thread(bound.build, run_id, parsed)`. As with ssh,
the builder must be `RemoteLibvirtBuild`; any other runtime builder selecting this host is
`NOT_IMPLEMENTED`. The provision/teardown is a patchable seam
(`ephemeral_build_session = EphemeralBuildVm.session`) so tests substitute a fake VM with no
libvirt. The capacity lease is released **on success only** (failure retains it for retry —
unchanged from ADR-0099; the reconciler reclaims on job-terminal).

### 4.5 Selection + admission

`services/runs/build_host_selection.py:resolve_and_admit` — the provenance cross-check
generalizes: a **non-local** host requires a git source (today's check is `kind == 'ssh' and
not git`; change to `kind != 'local' and not git`). Capacity gating already keys on
`kind != 'local'`, so `ephemeral_libvirt` acquires a lease under `BUILD_HOST` with no further
change. Disabled / unreachable rejection is unchanged.

### 4.6 Reconciler reaping

New provider port `BuildVmReaper` (`providers/remote_libvirt/build_vm_reaper.py`), mirroring
`RemoteLibvirtDumpVolumeReaper`: `list_build_vms() -> list[BuildVm(domain_name, run_id)]`
(matched by the `kdive-build-<uuid>` regex) and `delete_build_vm(domain_name)` (destroy +
undefine + delete overlay; absent = achieved post-state). The reconciler step
(`reconciler/build_hosts.py`, alongside `reclaim_orphan_build_host_leases`):

```
for vm in await reaper.list_build_vms():
    if not await build_job_is_live(conn, vm.run_id):   # queued/running BUILD job for run_id?
        await reaper.delete_build_vm(vm.domain_name)    # reap the VM ...
await reclaim_orphan_build_host_leases(conn)            # ... BEFORE freeing the slot
```

The job-liveness guard is the **same** predicate the lease reclaim uses (a build can run up to
`MAKE_TIMEOUT_S`, so age-based reaping would tear down a live build). The reaper is the narrow
libvirt seam; the DB guard lives in the reconciler (consistent with the dump-volume reaper).

**Ordering: reap the VM before reclaiming its lease.** The two cleanups are otherwise
independent sweeps, so the reconciler runs the VM reap **before** `reclaim_orphan_build_host_leases`
in the same tick. Freeing the lease slot first would let a new build admit and provision an
additional VM while the leaked one is still running, briefly oversubscribing the host past
`max_concurrent` (a crash-leak-only window; the success path already deletes the VM in the
handler's `finally` before releasing the lease). With VM-reap-first, a freed slot never
coexists with a live leaked VM.

## 5. Schema (migration 0029)

Additive / forward-only (ADR-0015):

```sql
ALTER TABLE build_hosts DROP CONSTRAINT build_hosts_kind_check;
ALTER TABLE build_hosts ADD CONSTRAINT build_hosts_kind_check
    CHECK (kind IN ('local', 'ssh', 'ephemeral_libvirt'));
ALTER TABLE build_hosts ADD COLUMN base_image_volume text;
ALTER TABLE build_hosts DROP CONSTRAINT build_hosts_ssh_fields_check;
ALTER TABLE build_hosts ADD CONSTRAINT build_hosts_fields_check CHECK (
    (kind = 'local'            AND address IS NULL     AND ssh_credential_ref IS NULL     AND base_image_volume IS NULL) OR
    (kind = 'ssh'              AND address IS NOT NULL AND ssh_credential_ref IS NOT NULL AND base_image_volume IS NULL) OR
    (kind = 'ephemeral_libvirt' AND address IS NULL    AND ssh_credential_ref IS NULL     AND base_image_volume IS NOT NULL)
);
```

`BuildHost` (dataclass) gains `base_image_volume: str | None`; `_row_to_host` maps it; the
`SELECT *` paths already return it. `workspace_root` for an `ephemeral_libvirt` row is the
**in-guest** build path; `max_concurrent` caps in-flight **leases** (≈ live builds), enforced
by the existing lease seam. It bounds concurrent *VMs* exactly on the success path (handler
tears the VM down before releasing the lease) and on the reconciler path (VM reaped before the
slot is freed, §4.6); a crash-leaked VM can transiently exceed it until the next reaper sweep,
so operators should size host headroom above `max_concurrent`.

## 6. Registration

`build_hosts.register` gains an optional `kind` argument (default `'ssh'`,
back-compatible) ∈ {`ssh`, `ephemeral_libvirt`}:

- `ssh`: requires `address` + a non-blank `ssh_credential_ref` (unchanged); `base_image_volume`
  must be absent.
- `ephemeral_libvirt`: requires `base_image_volume`; `address` / `ssh_credential_ref` must be
  absent (rejected `configuration_error` if supplied — an ephemeral host has no SSH credential).

`PLATFORM_ADMIN` + audit unchanged; the audit row never carries secret bytes (an ephemeral row
has none). `worker-local` seed protection, `list`, `disable`, `remove` unchanged.

## 7. Secrets, redaction, security

- A private git remote's credential resolves at the worker boundary, registers into the
  redaction registry, persists only `(present, source-ref)` (the ADR-0099 / #077 contract).
- The presigned PUT URL is a bearer capability: `upload_file` registers it in the redaction
  registry **before** the in-guest `curl` and redacts all captured output; an error detail
  uses the query-stripped form (`redact_presigned`), never the live URL.
- The TLS client cert/key is consumed by the libvirt transport layer and never reaches the
  exec seam (ADR-0077/0078), so it cannot appear in a transcript.
- Build execution is not added to the destructive-op gate (consistent with ADR-0099 §7.4);
  bounding controls are admin-only host registration, an ephemeral single-build VM, the
  component-root allowlist gating config/patch, worker-composed shell-quoted fixed argv, and
  the `make` timeout.
- **Egress dependency.** The build VM publishes artifacts via in-guest presigned PUT — the
  same guest→object-store hop the doctor egress probe exists to diagnose (a host `FORWARD DROP`
  silently breaks it, ADR-0091). A blocked path surfaces as a publish failure
  (`INFRASTRUCTURE_FAILURE`); its error detail should name the guest→object-store egress
  dependency (and the redacted object URL) so the operator is pointed at the `FORWARD`/network
  policy rather than misreading it as a build bug.

## 8. Testing (boundaries; no real libvirt in unit tests)

- **`GuestExecBuildTransport`:** a fake `agent_command` records the JSON guest-exec/-status
  round-trips and returns canned replies. Assert: `run` composes `/bin/sh -c "cd <q> && exec
  <join>"` with `{'/bin/sh'}` allowlisted and the call's `timeout_s`; `read_bytes`/`read_text`
  base64 round-trip + size cap; `write_bytes` composes the `printf … | base64 -d > path`
  pipeline directly (not via `_run_remote`'s `exec`-join form) and round-trips the bytes;
  `clone` issues init+fetch+checkout argv with arg validation; `upload_file` curl argv + ETag
  parse and registers the presigned URL in the redaction registry before the exec; a non-zero
  exit and a timeout map to the same categories as ssh.
- **`ShellBuildTransport` refactor:** the existing `SshBuildTransport` tests stay green
  (behavior-preserving); a parity test asserts both subclasses produce the same orchestrator
  argv for `read_bytes`/`clone`/`upload_file` given the same `_run_remote`.
- **Shared-namespace invariant:** a `kdive-build-<run_id>` domain does not appear in
  `used_gdb_ports` (no recorded gdbstub port) and its overlay name does not match the System
  overlay scheme or the host_dump volume reaper regex.
- **`EphemeralBuildVm`:** a fake provision-connection (like the provisioning tests) — assert
  overlay-over-`base_image_volume`, define+start of `kdive-build-<run_id>`, `wait_for_agent`,
  teardown destroys+undefines+deletes the overlay, idempotent provision, and teardown runs in
  `finally` even when the yielded body raises.
- **BUILD handler ephemeral branch:** fake builder + fake `ephemeral_build_session` — assert
  provision→build→teardown order; teardown on build failure; lease released on success only;
  a non-`RemoteLibvirtBuild` runtime builder → `NOT_IMPLEMENTED`; a worker-crash before
  teardown leaks the domain (covered by the reaper test, below).
- **Selection:** `ephemeral_libvirt` + warm-tree string → `configuration_error`;
  `ephemeral_libvirt` + git → lease acquired; at ceiling → `capacity_exhausted`
  synchronously; disabled/unreachable → `configuration_error`.
- **Registration:** `kind='ephemeral_libvirt'` without `base_image_volume` →
  `configuration_error`; with `address`/`ssh_credential_ref` → `configuration_error`; happy
  path inserts the row; non-admin → `authorization_denied`; audit row carries no secret;
  `kind='ssh'` default path unchanged.
- **Reconciler reaper:** a `kdive-build-<run_id>` domain whose BUILD job is terminal/gone is
  reaped; one whose BUILD job is live (queued/running) is **not** reaped (no age-based reap);
  a non-matching domain name is ignored.
- **Migration 0029:** schema test — `kind` CHECK admits `ephemeral_libvirt`; the per-kind field
  CHECK rejects an ephemeral row without `base_image_volume` and an ssh row with one;
  `base_image_volume` column present; `worker-local` seed still present and `local`-valid.
- **Live (`live_vm`):** real provision → in-guest `make` → publish → teardown on the
  operator-staged base build image. Not in CI.
