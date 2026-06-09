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
  matching the ADR-0081 bundle layout), and **add-or-replace a single deterministic grub entry**
  (the `kdive` slot) whose kernel cmdline is `<cmdline>` verbatim. It does **not** change the
  boot selection. For `--method kdump` it also enables the in-guest kdump capture service; for
  other methods it does not. Exit non-zero on any step's failure.
- `boot-id` — print `/proc/sys/kernel/random/boot_id` (the kernel-minted per-boot UUID).
- `boot` — select the `kdive` slot for the **next** boot only (grub one-shot) **and** trigger a
  **detached** reboot/kexec into it, atomically: the selection and the reboot are one in-guest
  action, so an intervening reboot cannot consume the selection before the reboot it pairs with.
  The reboot is detached (scheduled so the helper returns before the agent is torn down) — but
  the worker does not depend on a clean exit (see §3).

The worker **never** sends a shell string: each subcommand is a fixed argv whose `argv[0]` is the
helper path, so the in-guest program is allowlisted and arguments cannot be reinterpreted by a
shell (the ADR-0078 §2 / issue-3 constraint).

**Single-slot, last-install-wins.** The `install` subcommand replaces one deterministic
per-System grub slot rather than appending a per-Run entry. This is required, not incidental:
the `boot(system_id)` port carries **no `run_id`** (only the System id), so `boot()` cannot name a
per-Run entry — exactly as the local plane's `boot()` power-cycles whatever kernel the domain XML
names. "The `kdive` slot = the most recently installed kernel" is the same last-install-wins
semantics, and the per-System advisory lock the boot handler already holds
(`jobs/handlers/runs.py`) serializes install/boot for a System, so the single slot is race-safe.
Replacing (not appending) the slot also makes `install` **idempotent**: a re-run after an
abandoned step claim converges to the same `/boot` + grub state instead of duplicating entries.

### 2. `install()` pulls + installs in-guest via the registered-URL seam

`install(request)`:

1. Mints a **single** presigned GET for `request.kernel_ref` (one object, ADR-0081 §Consequences).
   The expiry is sized to cover a **worst-case in-guest bundle download** (the bundle is commonly
   hundreds of MB before gzip, ADR-0081), not the shortest possible window — a deliberate
   exception to "shortest expiry," still one object and one capability. If the URL expires
   mid-download the in-guest `curl` fails and the helper exits non-zero → `INSTALL_FAILURE`, which
   is recoverable: a re-run mints a fresh URL.
2. Runs `install --url … --cmdline <request.cmdline> --method <request.method>` through the
   issue-3 **`InTargetArtifactChannel`**, which registers the URL in the redaction registry
   **before** the exec, redacts+persists the captured transcript by exact value, and releases the
   per-op scope only after the persist (ADR-0078 §2). The cmdline is `request.cmdline` unchanged —
   the method-conditional crashkernel was already composed upstream (`cmdline_for`), so the
   crashkernel token is present iff `request.method` is `kdump`.
3. Maps a non-zero helper exit to `INSTALL_FAILURE`; a guest-agent/transport fault propagates as
   the seam's `TRANSPORT_FAILURE`; a vanished `kernel_ref` object surfaces as the store's
   `STALE_HANDLE`.

`install()` does **not** reboot or change the boot selection — it stages the bundle and replaces
the `kdive` grub slot, mirroring the local plane's split so the install handler's `install` step
records "kernel staged" and the boot handler's `boot` step owns the select+power transition.

### 3. `boot()` proves a fresh boot by boot-id change

With no shared console, `boot(system_id)` proves the reboot happened and reached usable userspace
by watching the kernel-minted **boot_id** change:

1. Read the pre-reboot `boot-id` through the guest agent (a baseline; a clean round-trip, no
   reboot yet).
2. Run `boot` (select the `kdive` slot + detached reboot) through the guest agent. The reboot
   tears down the guest agent, so this command may **not** return a clean exit — its
   guest-exec-status poll can hit an unreachable agent (`TRANSPORT_FAILURE`). That is the
   **expected** signal the reboot took effect, **not** a failure: `boot()` swallows a transport
   error / non-clean exit from the `boot` command and proceeds to the poll. Readiness never
   depends on the reboot command's exit status — only on the boot_id change below.
3. Poll `boot-id`: while the agent is unreachable (the expected window while the guest is down),
   keep waiting; once it answers with a boot_id **different** from the baseline, the System
   rebooted and the guest agent is back up — **ready**.
4. The agent never returning a changed boot_id within the bounded boot window is `BOOT_TIMEOUT`
   (a panic/hang on the new kernel manifests as the agent never reconnecting).

boot_id change is the readiness signal because it is falsifiable (a stale agent connection that
survived cannot fake a new boot_id) and self-contained (no cross-call state, no expected-kernel
identity — which the `boot(system_id)` port does not carry). boot() persists no transcript,
matching the local plane (its readiness poll is host-observational, not an artifact).

### 4. Verification (distinct from the readiness gate)

The boot_id readiness gate proves *a fresh boot reached agent-up*, which is necessary but does not
by itself verify the issue's acceptance criterion ("a built kernel becomes the booted kernel … the
crashkernel cmdline is present for kdump-method installs only"). That criterion is verified
separately:

- **Unit (no host):** assert `install()` composes the helper argv carrying `request.cmdline`, and
  that the cmdline carries the `crashkernel=` token **iff** `request.method` is `kdump` (the
  upstream `cmdline_for` composition is exercised at the install boundary), plus every error-path
  mapping (`INSTALL_FAILURE` / `TRANSPORT_FAILURE` / `STALE_HANDLE`, lost-agent-on-reboot tolerated,
  `BOOT_TIMEOUT`).
- **`live_vm` (real host):** after install+boot, read the guest's `/proc/cmdline` and confirm the
  `crashkernel=` token is present for a kdump-method install and absent otherwise, and confirm the
  running kernel is the built one (kernel version / build-id match). This closes the
  "boot_id changed ≠ our kernel" gap at the live tier; the in-guest single-slot last-install-wins
  selection is what makes the fresh boot our kernel.

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
- **`install` is idempotent and `boot` is selection-atomic.** Replacing the single `kdive` grub
  slot (not appending) makes a re-run after an abandoned step claim converge to the same state; the
  `boot` subcommand selecting the slot and rebooting as one in-guest action means readiness cannot
  be confused by a reboot that occurred between two separate worker jobs.
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
- **Select the boot entry in `install()` (one-shot) and reboot in `boot()`** as two separate
  in-guest actions. Rejected: the selection and the reboot land in two separate worker jobs, so an
  intervening reboot (a crash, a kdump cycle, an operator reset) between them can consume a
  one-shot selection booting our kernel once, after which `boot()`'s own reboot boots the *old*
  default while boot_id still changes — the System runs the wrong kernel but the Run reads
  "booted." Making the `boot` subcommand select-and-reboot atomically removes the window. (This is
  why selection lives in `boot`, not `install`.)
- **A persistent default-selection** (set the new entry as the permanent grub default) so the
  selection survives across calls. Rejected: a kernel that panics on boot would then boot-loop the
  System on every subsequent power event. A one-shot selection set atomically with the reboot is
  "try this kernel once" — a bad kernel falls back to the previous default on the next boot.
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
