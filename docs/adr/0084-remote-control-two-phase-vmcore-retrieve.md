# ADR 0084 — Remote control + two-phase vmcore retrieve (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0078](0078-object-store-in-target-install-seam.md)
  (the object-store + presigned-URL in-target seam this realizes for the Retrieve plane — the
  two-phase vmcore decision originates there), [ADR-0076](0076-remote-libvirt-provider-package.md)
  (the independent `remote_libvirt` package + the portability diff gate this stays inside),
  [ADR-0080](0080-remote-provisioning-disk-image-profile.md) (the provisioned disk-image base OS
  — qemu-guest-agent + virtio-serial — this controls and captures from),
  [ADR-0082](0082-remote-install-in-guest-kernel.md) (the single-allowlisted-helper + registered-URL
  `InTargetArtifactChannel` pattern this reuses), [ADR-0028](0028-control-plane-power-force-crash.md) (the Controller
  port + libvirt power/force_crash semantics this re-realizes over TLS),
  [ADR-0031](0031-retrieve-plane-vmcore-postmortem.md) (the Retriever/CrashPostmortem port pair and
  the worker-side `crash` postmortem this reroutes), [ADR-0033](0033-drgn-introspection-from-vmcore.md) /
  [ADR-0083](0083-remote-connect-debug-plane.md) (the `providers/debug_common/` provider-neutral
  worker-side layer this extends with the shared crash postmortem).
- **Spec:** [`../specs/m2-remote-libvirt.md`](../design/m2-remote-libvirt.md) §Decomposition issue 7.

## Context

`local_libvirt` realizes the Control and Retrieve planes against a hypervisor it shares a
host with: `LocalLibvirtControl` drives `libvirt.open(qemu:///system)` directly, and
`LocalLibvirtRetrieve` reads a kdump core from the host's dump path (or pulls a host-side
QEMU memory dump) and runs `crash` on the worker, which shares that filesystem.

`remote_libvirt` (ADR-0076/0078) shares no filesystem with its host. Control must drive
`qemu+tls://` through the mutual-TLS materialize→connect→cleanup lifecycle
(`remote_connection`, ADR-0077), and the capture kernel — a minimal kdump initramfs — is not
assumed to reach the object store, so a host-side read of the core is impossible. ADR-0078
already fixed the answer at the milestone level: **two-phase vmcore retrieve** — kdump writes
the core to the guest's local dump storage on crash; on the next normal boot the in-guest
agent uploads it to a presigned PUT, and the worker references the object. Issue 7 is the
realization of that decision plus the matching Control plane.

Three things the foundation left open for this issue:

1. **The Controller port** is still the `UnimplementedController` stub (composition wires
   `controller=UnimplementedController()`).
2. **The Retriever/CrashPostmortem port** is still the `UnimplementedRetriever` stub
   (`retriever`/`crash_postmortem` both point at it), and `supported_capture_methods` is the
   empty set, so `vmcore.fetch` admits no capture for a remote System.
3. The **`presign_put` direction** of the in-target seam (ADR-0078 step 4) has a publisher
   (the worker mints it) but no consumer; the PUT side of the artifact channel is unexercised.

The Controller/Retriever/CrashPostmortem port signatures are carried invariants (ADR-0076 §1):
`power(domain_name, action)`, `force_crash(domain_name)`, `capture(system_id, method)`,
`run_crash_postmortem(...)` must not change, and no core file may be touched outside the
ADR-0076 allowlist (the gate's core set excludes `providers/`, so the provider classes and the
composition map entry are free; only `supported_capture_methods` and the port wiring move).

## Decision

### 1. `RemoteLibvirtControl` realizes Control over `qemu+tls://`

`RemoteLibvirtControl` implements the `Controller` port against the remote host, driving the
same libvirt domain operations as `LocalLibvirtControl` — the domain calls (`create`/`destroy`/
`reset`/`reboot`/`injectNMI`) are transport-agnostic; only the connection lifecycle differs:

- `power(domain_name, action)`: `on→create`, `off→destroy`, `reset→reset`, `cycle→reboot`.
  `on`/`off` swallow libvirt's "already in the target state" error
  (`VIR_ERR_OPERATION_INVALID`) as the achieved post-state (idempotent); an absent domain or
  any other libvirt error is `CONTROL_FAILURE`. Identical contract to the local plane.
- `force_crash(domain_name)`: `injectNMI(0)`. The disk-image base OS is configured to panic on
  an unknown NMI (`kernel.unknown_nmi_panic=1`) so the NMI drives the panic→kdump path — a
  base-image obligation, the Control-plane sibling of ADR-0080's qemu-guest-agent obligation
  and ADR-0082's `kdive-install-kernel` obligation. An absent domain or libvirt error is
  `CONTROL_FAILURE`.

The connection is opened per op through `remote_connection(config, secret_backend,
open_connection=…)` and closed (with its pkipath) on every exit path. The plane is DB-free and
carries no `run_id`; it is keyed on the provider domain name (`domain_name_for(system_id)`),
exactly as the generic `control.*` handlers drive it. **No shared layer with `local_libvirt`**
(ADR-0076): the ~small power/force_crash body is reimplemented over `remote_connection` rather
than abstracted, matching the deliberate fake-duplication the remote test suite already uses.

### 2. `RemoteLibvirtRetrieve.capture()` — two-phase, KDUMP-only

`supported_capture_methods` widens from `frozenset()` to `frozenset({CaptureMethod.KDUMP})`.
**`HOST_DUMP` is not supported on remote**: a host-dump reads QEMU guest memory through a
host-side path the worker would have to share, which is exactly the filesystem coupling the
remote provider does not have. `capture()` rejects a non-KDUMP method with
`CONFIGURATION_ERROR` defensively (the empty-set → `{KDUMP}` widening already blocks it at
`vmcore.fetch`).

`capture(system_id, KDUMP)` runs **after** a crash, against a System whose control-plane state is
`crashed` but whose guest must have completed its kdump-triggered reboot back to the normal kernel
for the in-guest agent to be reachable (see §0 below). It moves the core out of the guest in three
guest-agent round-trips (the digest must be known before the PUT can be signed, so inspect precedes
presign precedes upload):

0. **Readiness wait.** The capture is admitted by `vmcore.fetch` the moment the System is `crashed`
   (§4), which can be *before* the guest finishes rebooting out of the capture kernel — so the
   first inspect can hit an unreachable agent. `capture()` polls inspect over a bounded
   capture-readiness window: an unreachable agent (`TRANSPORT_FAILURE`) is treated as "still
   rebooting, keep polling" (the ADR-0082 `_REBOOT_EXPECTED` shape), not a failure. Exhausting the
   window without a reachable agent → `READINESS_FAILURE`. This mirrors install's boot-id poll: the
   `crashed` control state and "guest agent back up" are decoupled in time, so capture cannot assume
   the guest is reachable on the first call.
1. **Inspect.** Run the single allowlisted retrieve helper —
   **`/usr/local/sbin/kdive-capture-vmcore inspect`** (the base image's Retrieve-plane contract,
   the only program this plane allowlists in `GuestAgentExec`) — which prints one JSON object:
   `{present, sha256 (base64), size_bytes, build_id, dmesg_b64}` for the local kdump core. The
   helper **bounds `dmesg_b64`** to a fixed byte cap (truncation marked) so the inspect reply stays
   within the qemu-guest-agent maximum response regardless of the guest's ring-buffer size — an
   oversized dmesg must not block obtaining the sha256/size the upload needs.
   `present=false` (no core in the guest's dump storage) → `READINESS_FAILURE`. A malformed
   reply → `INFRASTRUCTURE_FAILURE`. `size_bytes` over the single-PUT 5 GiB ceiling →
   `CONFIGURATION_ERROR` (multipart upload is a follow-up, the ADR-0048 §>5 GiB shape). `size_bytes`
   is the guest's reported size used for the ceiling check; the **signed sha256** (not the size) is
   the integrity binding S3 enforces on the body.
2. **Presign.** The worker constructs the **deterministic raw object key**
   `artifact_key("remote-libvirt", "systems", system_id, "vmcore-kdump")` — the same key
   `vmcore.fetch`'s `raw_vmcore_key`/`captured_method` parse and the same first-method-wins key
   the handler's idempotency turns on (ADR-0050) — and mints `presign_put(PresignPutRequest(key,
   sha256, size_bytes, sensitivity=SENSITIVE, retention_class="vmcore", expires_in=<covers the
   upload>))`. S3 signs the checksum into the URL, so the guest's PUT body must hash to exactly
   the inspected `sha256` or S3 rejects it — the core cannot be swapped between inspect and
   upload.
3. **Upload.** Register the PUT URL for redaction and run
   `kdive-capture-vmcore upload --url <put> --header <k:v>…` through the issue-3
   `InTargetArtifactChannel`, which masks the bearer URL by exact value in the persisted
   transcript and releases the per-op scope only after redact-and-persist (ADR-0078 §2). The
   in-guest helper `curl --upload-file`s the local core with the signed checksum + metadata
   headers. A non-zero helper exit (curl 403 on an expired URL, an S3 checksum rejection, a read
   error) → `INFRASTRUCTURE_FAILURE` (the spec's mapping for presigned-URL / object-store /
   host-infra failures); an unreachable agent → `TRANSPORT_FAILURE`.

The worker then **references** the uploaded object without re-downloading it:
`head(raw_key)` confirms it landed and returns the `etag`. A `None` head after a success-reporting
upload → `INFRASTRUCTURE_FAILURE`. Integrity is already guaranteed by the **signed-checksum PUT**
(S3 rejects a body that does not hash to the URL's signed `sha256`), so `head` is for presence +
etag recovery; `HeadResult.checksum_sha256` is `str | None` (only populated when the store echoes a
written checksum, which MinIO and S3 do not do identically), so the worker compares it to the
inspected `sha256` **only when present** and never hard-fails provenance on a `None` the store
simply did not return. The result is a `StoredArtifact(raw_key, etag, SENSITIVE, "vmcore")`.

The **redacted derivative** travels **inline**: the worker base64-decodes `dmesg_b64`, runs its
own `Redactor` over it (defense in depth — redaction happens worker-side where the registry
lives, not trusted to the guest), and `put_artifact`s it as `vmcore-kdump-redacted` (REDACTED,
"vmcore"). Only the large raw core takes the presigned-PUT path; the small dmesg does not earn a
second bearer capability.

`capture()` returns `CaptureOutput(raw, redacted, vmcore_build_id=build_id)`. The generic
`capture_handler` (`jobs/handlers/vmcore.py`) owns the per-System lock, the first-method-wins
precheck, and the artifact-row inserts — unchanged. Re-running `capture()` after an abandoned
job converges: the key is deterministic and the S3 PUT overwrites with the same checksum, so the
operation is idempotent.

### 3. `RemoteLibvirtRetrieve.run_crash_postmortem()` — shared worker-side helper

Once issue 7 lands a captured core in the object store, the worker-side `crash` postmortem is
**identical** for local and remote: fetch the core + debuginfo from S3, verify the core's
build-id matches the Run's `expected_build_id` (provenance — this is where "the vmcore matches
the Run build-id" is enforced), stage both to temp files, run a validated `crash` command batch
over an injected subprocess seam, and return the **redacted** transcript. That logic is
extracted out of `LocalLibvirtRetrieve` into a provider-neutral helper in
**`providers/debug_common/crash_postmortem.py`** (the ADR-0083 home for shared worker-side
postmortem code, alongside the drgn `introspect` helpers), parameterized by injected
`fetch_object` / `run_crash` / `read_build_id` seams and the secret registry. Both
`LocalLibvirtRetrieve` and `RemoteLibvirtRetrieve` delegate to it; neither owns a private copy.

This replaces the `UnimplementedRetriever` entirely (Replace-don't-deprecate): composition wires
`retriever`/`crash_postmortem` to the one `RemoteLibvirtRetrieve`, and `planes.py`'s two stubs
are deleted.

### 4. force_crash → kdump → capture is the end-to-end, driven by the existing operator tool

The acceptance flow uses only existing orchestration, but the capture is **operator-initiated, not
auto-scheduled**: `control.force_crash` injects the NMI → the guest panics → the control handler
transitions the System to `crashed` → kdump writes the core to local dump storage and reboots the
guest normally → an operator (or the `live_vm` acceptance test) calls `vmcore.fetch(system_id,
kdump)`, which admits a `CAPTURE_VMCORE` job on the `crashed` System
(`mcp/tools/lifecycle/vmcore.py`) → the worker runs the two-phase `capture()`. **No component
auto-runs capture after the reboot**, and the `crashed` admission state does not imply the guest
agent is back up — which is exactly why `capture()` owns the §2 step-0 readiness wait rather than
assuming a reachable guest. No new handler, job kind, tool, or state edge — the provider supplies
the Control and Retrieve seams the unchanged handlers already call.

All slow/host seams — the TLS connection opener, the guest-agent round-trip, the object store,
the clock, sleep, the `crash` subprocess — are injected, so unit tests drive the full
orchestration and every error path with no libvirt host or S3; the real
NMI/curl/upload/`crash` mechanics run only under the `live_vm` gate.

## Consequences

- **Zero core/port change.** `RemoteLibvirtControl` and `RemoteLibvirtRetrieve` satisfy the
  unchanged Controller/Retriever/CrashPostmortem ports and slot into the generic `control.*` and
  `vmcore.*` handlers; only `providers/remote_libvirt/`, `providers/debug_common/`,
  `providers/local_libvirt/retrieve.py` (the delegation), and the composition map entry change —
  all outside the portability gate's core set, so it stays green.
- **The two-phase model is the M3–M5 carry-forward.** A target that pulls its kernel and pushes
  its vmcore through bounded presigned URLs is the cloud/bare-metal model; only the in-target
  execution transport (guest-agent → cloud-init/SSH) changes behind the same Retriever contract.
- **Remote captures KDUMP only.** Host-dump is a host-coupled capability; advertising only KDUMP
  keeps `vmcore.fetch` from admitting a method that cannot work remotely, rather than failing it
  in a stub. Local keeps both.
- **The PUT side of the in-target seam is now exercised.** Issue 3 proved registered-URL
  redaction on a GET (install); issue 7 proves it on a PUT (vmcore upload) — the same
  one-object-capability, register-before-exec, release-after-persist contract on the write
  direction.
- **New base-image obligation.** The disk image carries `/usr/local/sbin/kdive-capture-vmcore`
  (with `curl` and a kdump-core reader available to it) and is configured for
  `kernel.unknown_nmi_panic=1`, in addition to the ADR-0080/0082 obligations.
- **Build-id match is verified at postmortem, not capture.** `capture()` records the inspected
  build-id but stores the core unconditionally; the "matches the Run build-id" acceptance criterion
  is enforced by `run_crash_postmortem`'s provenance gate, which the `live_vm` acceptance test drives
  explicitly, because the `capture(system_id, method)` port carries no Run identity. The vmcore is
  scoped **per System** (first-method-wins, ADR-0050), not per Run, so a core captured under one Run
  can satisfy a later Run's precheck; the build-id gate at postmortem is what ties a given core to a
  given Run's kernel, and the acceptance test asserts that match rather than assuming capture
  guarantees it.
- **Single-PUT ceiling.** A core over 5 GiB is rejected at inspect (`CONFIGURATION_ERROR`);
  multipart upload for very large cores is a deferred follow-up, not an M2 widening.

## Considered & rejected

- **Reuse `LocalLibvirtControl`/`LocalLibvirtRetrieve` with a different connection opener.**
  Rejected: ADR-0076 fixes the remote package as independent of local (the test suite already
  duplicates fakes deliberately), and the connection lifecycle genuinely differs — remote needs
  the mutual-TLS materialize→pkipath→cleanup of `remote_connection`, not `libvirt.open(uri)`.
  The shared piece that *is* worker-side and provider-neutral — the `crash` postmortem — is the
  one thing extracted (into `debug_common`), matching where ADR-0083 already put shared
  worker-side debug code.
- **Have the guest upload the redacted dmesg via a second presigned PUT** (symmetric with the
  raw core). Rejected: it doubles the bearer capabilities minted per capture and moves the
  redaction trust boundary into the guest helper; the dmesg is small enough to return inline and
  redact worker-side, where the secret registry lives and the redaction is auditable.
- **Mint the presigned PUT before inspecting the core** (skip the inspect round-trip). Rejected:
  `presign_put` signs the checksum into the URL (S3 rejects a mismatching body), so the worker
  must learn the core's sha256 before it can sign — the inspect phase is not optional. Signing
  the checksum is also what makes "the object is the core the guest inspected" verifiable.
- **Trust the guest's upload-success exit and skip the `head` reference.** Rejected: a helper
  that exits 0 without a durable object (a buffered curl, a proxy 200) would leave a dangling
  artifact row. The `head` confirms the object landed and recovers the etag the `StoredArtifact`
  row needs without re-downloading hundreds of MB.
- **Capture the core synchronously inside `force_crash`** (one combined op). Rejected: the
  capture kernel cannot reach S3 (ADR-0078) and the core is only readable after the *next normal
  boot*, so crash and capture are necessarily two phases across a reboot — modeled by the
  existing `force_crash` control op and a later `CAPTURE_VMCORE` job, not one provider call.
- **Reboot/capture over the TLS control channel** (`virDomainCoreDump` to a host path).
  Rejected: it rebuilds the host-shared-filesystem model the remote provider exists to retire
  and does not generalize to cloud/bare-metal, where there is no controllable host to dump to.
