# ADR 0082 — Remote install: in-guest kernel install + boot-id readiness (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0078](0078-object-store-in-target-install-seam.md)
  (the object-store + presigned-URL in-target install/retrieve seam this realizes for the
  Install plane), [ADR-0076](0076-remote-libvirt-provider-package.md) (the independent
  `remote_libvirt` package + the portability diff gate this stays inside),
  [ADR-0080](0080-remote-provisioning-disk-image-profile.md) (the provisioned disk-image
  base OS — qemu-guest-agent + virtio-serial channel — this installs into),
  [ADR-0081](0081-remote-build-kernel-bundle.md) (the gzip vmlinuz+modules bundle this pulls
  and extracts in-guest), [ADR-0051](0051-install-method-conditional-crashkernel.md) /
  [ADR-0061](0061-boot-cmdline-composition.md) (the method-conditional crashkernel cmdline
  this writes into the guest grub), [ADR-0030](0030-install-boot-plane.md) (the
  Installer/Booter port pair this provider realizes for a remote target).
- **Spec:** [`../specs/m2-remote-libvirt.md`](../specs/m2-remote-libvirt.md) §Decomposition issue 5.

## Context

`local_libvirt` installs a kernel by staging the `bzImage` to a host-local path and redefining
the domain for QEMU direct-kernel boot, then power-cycles the domain and confirms readiness by
tailing a host-side **teed serial console log** for a `kdive-ready` marker (ADR-0030/0055). Both
the staging path and the console log live on a filesystem the worker shares with the hypervisor.

`remote_libvirt` (ADR-0078) shares no filesystem with its host. The kernel arrives as a
presigned-GET-pulled `.tar.gz` bundle (ADR-0081) the **target** installs in-guest, and there is
no host path to tee a console log to. So the Install plane needs two things the local plane gets
from the shared filesystem for free:

1. **An in-guest install mechanism** — pull the bundle, decompress+extract it
   (`boot/vmlinuz` → `/boot`, `lib/modules/<ver>` → `/lib/modules`), add a grub boot entry with
   the method-conditional crashkernel cmdline, and select it for the next boot — all in-guest,
   driven through the qemu-guest-agent exec seam (ADR-0078 §3, issue 3), because `qemu+tls://`
   offers no file transfer or command execution of its own (ADR-0077).
2. **A readiness signal without a shared console** — confirm the System actually rebooted into
   a kernel and came back far enough to be usable, with no host-side console log to classify.

The Installer/Booter port pair is a carried invariant (ADR-0076 §1): the install handler
(`jobs/handlers/runs.py`) already composes the cmdline (method-conditional crashkernel via
`services/runs/steps.cmdline_for`) and calls `installer.install(InstallRequest)` then
`booter.boot(system_id)` generically. The port signatures — `InstallRequest` (system_id, run_id,
kernel_ref, cmdline, method, initrd_ref) and `boot(system_id)` — must not change, and no core
file may be touched outside the ADR-0076 allowlist.

## Decision

`RemoteLibvirtInstall` realizes the `Installer` + `Booter` ports against the provisioned
disk-image System, splitting work across the two calls exactly as `LocalLibvirtInstall` does
(stage in `install`, power into the kernel in `boot`):

### 1. A single allowlisted in-guest helper with subcommands

The base image carries one operator-provided helper program,
**`/usr/local/sbin/kdive-install-kernel`** (the base-image contract, alongside the
qemu-guest-agent of ADR-0080). It is the **only** program the remote Install/Boot plane
allowlists in `GuestAgentExec` (issue 3), so the guest-agent enforcement surface stays one
fixed program. It exposes three subcommands the worker composes argv for:

- `install --url <presigned-get> --cmdline <cmdline> --method <method>` — `curl` the bundle from
  the presigned URL, `tar xzf` it (`boot/vmlinuz` → `/boot`, `lib/modules/<ver>` → `/lib/modules`,
  matching the ADR-0081 bundle layout), add a grub entry whose kernel cmdline is `<cmdline>`
  verbatim, and select that entry for the **next** boot only (grub one-shot). For
  `--method kdump` it also enables the in-guest kdump capture service; for other methods it does
  not. Exit non-zero on any step's failure.
- `boot-id` — print `/proc/sys/kernel/random/boot_id` (the kernel-minted per-boot UUID).
- `reboot` — trigger the reboot/kexec into the selected entry.

The worker **never** sends a shell string: each subcommand is a fixed argv whose `argv[0]` is the
helper path, so the in-guest program is allowlisted and arguments cannot be reinterpreted by a
shell (the ADR-0078 §2 / issue-3 constraint).

### 2. `install()` pulls + installs in-guest via the registered-URL seam

`install(request)`:

1. Mints a **single** presigned GET for `request.kernel_ref` (one object, ADR-0081 §Consequences),
   bounded to the op's lifetime.
2. Runs `install --url … --cmdline <request.cmdline> --method <request.method>` through the
   issue-3 **`InTargetArtifactChannel`**, which registers the URL in the redaction registry
   **before** the exec, redacts+persists the captured transcript by exact value, and releases the
   per-op scope only after the persist (ADR-0078 §2). The cmdline is `request.cmdline` unchanged —
   the method-conditional crashkernel was already composed upstream (`cmdline_for`), so the
   crashkernel token is present iff `request.method` is `kdump`.
3. Maps a non-zero helper exit to `INSTALL_FAILURE`; a guest-agent/transport fault propagates as
   the seam's `TRANSPORT_FAILURE`; a vanished `kernel_ref` object surfaces as the store's
   `STALE_HANDLE`.

`install()` does **not** reboot — staging only, mirroring the local plane's split so the install
handler's `install` step records "kernel staged" and the boot handler's `boot` step owns the
power transition.

### 3. `boot()` proves a fresh boot by boot-id change

With no shared console, `boot(system_id)` proves the reboot happened and reached usable userspace
by watching the kernel-minted **boot_id** change:

1. Read the pre-reboot `boot-id` through the guest agent (a baseline).
2. Run `reboot` through the guest agent.
3. Poll `boot-id`: while the agent is unreachable (the expected window while the guest is down),
   keep waiting; once it answers with a boot_id **different** from the baseline, the System
   rebooted and the guest agent is back up — **ready**.
4. The agent never returning a changed boot_id within the bounded boot window is `BOOT_TIMEOUT`
   (a panic/hang on the new kernel manifests as the agent never reconnecting).

boot_id change is the readiness signal because it is falsifiable (a stale agent connection that
survived cannot fake a new boot_id) and self-contained (no cross-call state, no expected-kernel
identity — which the `boot(system_id)` port does not carry). boot() persists no transcript,
matching the local plane (its readiness poll is host-observational, not an artifact).

All slow/host seams — the TLS connection opener, the guest-agent round-trip, the clock, and sleep
— are injected, so unit tests drive the full install/boot orchestration and every error path with
no libvirt host; the real curl/tar/grub/reboot mechanics run only under the `live_vm` gate.

## Consequences

- **Zero core/port change.** `RemoteLibvirtInstall` satisfies the unchanged Installer/Booter
  ports and slots into the generic install/boot handlers; only `providers/remote_libvirt/` and the
  composition map entry change, so the portability gate stays green.
- **The in-guest install model is the M3–M5 carry-forward.** A single helper the target runs to
  pull+install+select+reboot is exactly what cloud-init (M3) and post-netboot SSH (M4/M5) re-realize
  behind the same Installer/Booter contract; only the in-target execution transport changes.
- **New base-image obligation.** The disk image must carry `/usr/local/sbin/kdive-install-kernel`
  (with `curl`, `tar`, and a grub editor available to it) in addition to the ADR-0080
  qemu-guest-agent. This is the same shape of obligation ADR-0080 already places on the image; the
  cloud/bare-metal equivalents are their milestones'.
- **Readiness is "a fresh boot reached agent-up," not "the expected kernel is running."** The
  `boot(system_id)` port carries no expected build-id, and a DB read would make a provider plane
  stateful, so M2 confirms a real reboot (boot_id change) into a guest whose agent answers; the
  install step's one-shot grub selection is what makes that fresh boot our kernel. Verifying the
  running kernel's identity against the build-id is a follow-up that would need a port field, not
  an M2 widening.
- **One presigned GET, one bearer capability.** Because the bundle is one object (ADR-0081), the
  installer registers and redacts exactly one URL per install — the ADR-0078 one-object capability
  shape — not two.
- **Cost: more in-guest moving parts than a host copy** (pull, decompress, grub edit, reboot,
  boot-id poll). Accepted: it is the model ADR-0078 fixes for every later provider.

## Considered & rejected

- **Compose the install as several primitive guest-exec calls** (`curl`, then `tar`, then a grub
  command, each its own allowlisted program). Rejected: it widens the guest-agent allowlist from
  one program to a toolbox, spreads a multi-step transaction with no in-guest rollback across many
  worker round-trips, and pushes install logic (extract layout, grub entry shape) into the worker
  where it cannot be tested without a guest. One helper keeps the allowlist minimal and the
  install transaction in-guest where it runs.
- **Reboot over the TLS control channel** (`virDomainReboot`) instead of through the guest agent.
  Rejected: the spec fixes the reboot as part of the in-target seam (ADR-0078 §3, issue 5 — "via
  the seam") so the one mechanism carries forward to M3–M5, where there is no libvirt control
  channel to reboot through. A control-channel reboot would be a libvirt-only path discarded one
  milestone later.
- **Confirm readiness by polling the guest-agent channel state alone** (as provisioning does,
  ADR-0080). Rejected: the agent is already connected before the reboot, so a bare "agent
  connected" poll can return immediately on the pre-reboot connection and never prove the new
  kernel booted. boot_id change is the smallest signal that proves a real boot transition.
- **Confirm readiness by re-teeing the guest serial console to the worker** over the TLS console
  channel and reusing the local `classify_console` marker logic. Rejected for M2: it rebuilds the
  host-console model the remote provider exists to retire (ADR-0078), and ties readiness to a
  console stream where the guest-agent round-trip the seam already uses is sufficient. The console
  path is the bare-metal SoL story (ADR-0079), not the disk-image one.
- **Verify the booted kernel's build-id** by adding the expected build-id to `boot(system_id)` or
  reading it from the run ledger in the provider. Rejected: the first changes a shared port for a
  remote-only need (against ADR-0076 §1); the second makes a provider plane read core state,
  breaking the DB-free provider contract. Deferred as a possible follow-up port field.
