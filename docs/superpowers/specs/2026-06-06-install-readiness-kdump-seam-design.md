# Install readiness + kdump-check seam — design (issue #127, gap G4)

- **Status:** Draft
- **Date:** 2026-06-06
- **Issue:** [#127](https://github.com/randomparity/kdive/issues/127) (gap **G4** of
  [#123](https://github.com/randomparity/kdive/issues/123)).
- **Depends on:** [ADR-0030](../../adr/0030-install-boot-plane.md) (the `Booter`/`Installer`
  ports, the injected `readiness`/`kdump_check` seams, and the
  `boot_timeout` vs `readiness_failure` split this fills), [ADR-0049](../../adr/0049-crash-capture-tiers.md)
  (the always-on console tier and the deferral of in-guest kdump to #115),
  [ADR-0052](../../adr/0052-bootable-rootfs-image-builder.md) (the `kdive-ready` console
  marker the rootfs emits).
- **ADR:** [ADR-0055](../../adr/0055-install-readiness-kdump-seam.md) (the open decisions this
  spec settles).
- **Port from:** `~/src/kdive-v1` `SubprocessLibvirtRunner.stream_console` + `ConsoleResult`
  (`ready`/`timeout`/`exited`, `virsh domstate` liveness poll, bounded snippet).

## 1. Problem

`src/kdive/providers/local_libvirt/install.py` has two placeholder seams that both
`raise MISSING_DEPENDENCY`:

- `_real_readiness(system_id)` — the run-readiness preflight `boot()` polls. `boot()` already
  power-cycles the domain and maps the seam's `ReadinessResult(answered, ok)` to
  `BOOT_TIMEOUT` (never answered) / `READINESS_FAILURE` (answered but a check failed); it just
  has no real probe.
- `_real_kdump_check(system_id)` — the kdump capture prerequisite, gated on
  `method == CaptureMethod.KDUMP`.

Without a real readiness probe there is no "verify the bug occurred" signal: the live demo
(#123) boots `~/src/linux` with the dcache fault (`dhash_entries=1`) and needs that boot to
resolve to **failure**, while the fixed kernel resolves to **ok**. This seam is what makes
the verification real.

## 2. Scope

In scope (one source file + tests):

- `src/kdive/providers/local_libvirt/install.py`
  - A pure, host-free console **classifier** that maps console bytes to a verdict
    (`ready` / `crashed` / `pending`).
  - `_real_readiness` — the `live_vm` tail wrapper that reads the teed console log, polls
    domain liveness, and maps the classifier verdict to `ReadinessResult`.
  - Replace the injected `kdump_check` seam with a host-observable initrd-presence check in
    `install()` (the staged kdump capture environment's carrier; §5).
- `tests/providers/local_libvirt/test_install.py` (+ `tests/providers/local_libvirt/fixtures/`)
  — unit tests for the classifier (every signature, the marker, pre-marker precedence,
  empty/malformed bytes), two committed console fixtures (crash + clean) classified in CI (§7),
  the kdump initrd-presence gate, and the existing `live_vm` `test_live_vm_real_install_boot`
  stub filled with the real boot-to-verdict acceptance (still `live_vm`-gated).

Out of scope: the `virsh domstate` subprocess and the console tail loop stay behind the
existing `live_vm` gate (no running host in CI); per-rootfs readiness-marker resolution
(the seam uses the demo's literal `kdive-ready`; §6); the deeper **in-guest** kdump probe
(kdump.service active, `makedumpfile` present, `kexec_crash_size > 0`) which needs the
deferred Tier-3 kdump guest image and a guest transport ([#115](https://github.com/randomparity/kdive/issues/115),
ADR-0049 §6); any change to `boot()`'s orchestration, the Run state machine, or the console
artifact registration (`runs.py` already records and redacts it).

## 3. The readiness verdict: a pure classifier over console bytes

The heart of the seam is a pure function — the unit-testable core, mirroring how the G2 fetch
seam's `_stage_object` is the unit-tested core under the `live_vm` `_real_fetch` wrapper:

```python
ConsoleVerdict = Literal["ready", "crashed", "pending"]

def classify_console(data: bytes, *, marker: str) -> ConsoleVerdict
```

- **`crashed`** — a kernel crash/stall signature (§4) appears in the **pre-marker** region of
  the console (all output before the first marker **line**, or the whole console when the marker
  line is absent). Resolved **first** (crash-wins precedence, §4.1).
- **`ready`** — a marker line is present and no crash signature precedes it.
- **`pending`** — neither; the guest is still booting (or the log is not yet written).

**The marker is matched as a whole line, not a bare substring.** ADR-0052's readiness unit is
named `kdive-ready.service` and its `ExecStart` writes the bare line `kdive-ready\n` directly to
`/dev/ttyS0`; with the demo cmdline (`console=ttyS0`, no `quiet`) systemd *also* prints
`Starting kdive-ready.service…` / `Started kdive-ready.service.` to that console — lines that
*contain* the substring `kdive-ready` but are **not** the signal. A substring match would fire on
the systemd unit-name line (declaring `ready` at unit-start, and truncating the pre-marker scan
region there). So the classifier matches the marker line-anchored — `(?m)^\s*kdive-ready\s*$` —
which the service's bare echo satisfies and the `kdive-ready.service` status lines do not.

The scan region matters: the matcher inspects only output **up to the first marker line**. A boot
that reached the `kdive-ready` line with no crash before it is `ready`; benign console text
*after* the marker (userspace service logs, later non-fatal kernel messages) cannot retroactively
flip a healthy boot to `crashed`. With the marker line absent the whole console is scanned, so a
crash that prevents the marker still resolves `crashed`.

**Precondition — the console log is per-boot-fresh (truncate-on-start).** Pre-marker scoping is
correct only if "the first marker line" is *this* boot's marker, not a stale one from a prior boot. It
is: `provisioning.py` emits `<serial><log file=…/>` with **no `append` attribute`**, and
QEMU/libvirt default the chardev `logappend` to off → the log is **truncated on each domain
`create()`**, which `boot()`'s power-cycle (destroy→create) always performs. So each boot starts
from an empty log and there is no stale marker. This is a load-bearing precondition, not an
assumption: it is guarded by a provisioning-XML regression test asserting the `<log>` element
carries no `append='on'` (§7). If `append='on'` were ever set, a stale prior-boot marker could
both falsely satisfy `ready` *and* hide a later crash in the unscanned post-marker region — so
the seam's correctness is explicitly tied to the truncate default (§8).

`data` is decoded `utf-8` with `errors="replace"` so a partial multibyte tail or non-UTF-8
console bytes never raise — they classify as `pending` until more output arrives. Empty bytes
(absent/unreadable log, handled by the existing `read_console_log`) classify as `pending`.

The verdict carries **no console text** — only the three-valued tag. The seam returns
`ReadinessResult(answered, ok)` (booleans), so no untrusted guest output crosses the seam
boundary and no redaction is required here; the console **snippet** the acceptance refers to
is captured separately and redacted by the `runs.boot` handler (ADR-0049 §4), independent of
this verdict.

## 4. Crash signatures

A fail-closed, fatal/stall-grade set of substrings, matched case-sensitively against the
decoded console. Word-boundary lookbehind guards the short tokens against benign substrings:

| Signature | Catches |
|-----------|---------|
| `Kernel panic` | `Kernel panic - not syncing` |
| `(?<![A-Za-z])BUG:` | `BUG()`, soft-lockup (`watchdog: BUG: soft lockup`), null-deref (`BUG: unable to handle kernel …`), KASAN report header; the lookbehind excludes `DEBUG:` |
| `(?<![A-Za-z])Oops:` | x86 oops header (`Oops: 0000 [#1]`) |
| `general protection fault` | `#GP` |
| `[Uu]nable to handle kernel` | x86/arm page-fault header |
| `KASAN:` | KASAN report (often co-occurs with `BUG:`; listed for clarity) |
| `KFENCE:` | KFENCE report |
| `detected stall` | RCU stall (`rcu: … self-detected stall on CPU`) |

The set is **non-exhaustive and additive**: a missed signature is a one-line addition, not a
contract change. The unit tests pin the *mechanism* (each listed signature → `crashed`; a
benign line containing `DEBUG:` does **not** crash; the marker → `ready`). The live_vm
acceptance (§7) is the falsifiable validation that the demo's **actual** `dhash_entries=1`
output is caught; if it is not, the fix is to add that signature.

### 4.1 Precedence: a crash *before* the marker wins

When a crash signature appears in the pre-marker region (§3), the verdict is **`crashed`** even
if the marker line later appears. This is fail-closed: the seam exists to produce a trustworthy
verification signal, and a false `ok` would defeat it. The demo's vulnerable boot crashes
*before* the rootfs emits the marker, so this is the common path, not a corner case.

Scoping the match to the pre-marker region (rather than the whole console) is what keeps a
*healthy* boot from a false `crashed`: a fixed kernel that reaches the `kdive-ready` line and then logs
benign console text containing a signature substring (a userspace service printing `BUG:`, a
later non-fatal kernel message) stays `ready`, because only pre-marker output is matched. The
residual case — a *non-fatal* signature substring emitted **before** the marker on an otherwise
healthy boot (e.g. a benign lockdep splat) — still resolves `crashed`; this is accepted as the
fail-closed cost of the settled crash-wins stance, and the §7 clean-boot fixture guards against
the common forms.

## 5. kdump-check: host-observable initrd presence

A *real* kdump preflight is irreducibly in-guest — v1 proves it (`prereqs/kdump_probe.py`
execs a probe script in the booted guest and judges `/sys/kernel/kexec_crash_size`,
`systemctl is-active kdump`, `/etc/kdump.conf`). That guest probe needs the Tier-3 kdump guest
image and a guest transport, both deferred to #115 (ADR-0049 §6). Implementing it now would be
speculative.

The one prerequisite checkable **on the host at install time** is the presence of a staged
**initrd**: a `crashkernel=` reservation is inert without a capture initramfs that actually
saves a core (ADR-0030 §4). So `install()` gates a kdump-method install on a staged initrd:

```python
if method is CaptureMethod.KDUMP and not _kdump_capture_present(initrd_path):
    raise CategorizedError(..., category=ErrorCategory.CONFIGURATION_ERROR)
```

where `_kdump_capture_present(initrd_path)` is `initrd_path is not None and
initrd_path.exists()`. This is **necessary but not sufficient** — it does not prove the initrd
is kdump-capable; the deeper guest-side verification lands with #115. The check is honest about
that scope rather than overstating a presence check as a full kdump readiness proof.

**Boundary — embedded-initramfs kdump kernels.** `install()` supports `initrd_ref=None` (a
bzImage with an embedded initramfs; `install.py` docstring §§ "When `initrd_ref` is `None`").
The host-presence gate requires a *separately staged* initrd, so a kdump-method install with an
embedded initramfs (`initrd_ref=None`) is rejected as `configuration_error`. This is an explicit
M0 boundary, not an oversight: the host cannot tell from `initrd_ref=None` whether a kdump
*capture* environment is embedded, and the demo path is `console`, not `kdump` — so M0's
coarse host gate requires the separate initrd, and the only verifier that can judge an embedded
capture environment (regardless of delivery) is #115's in-guest probe, which supersedes this
gate. A kdump kernel that embeds its capture initramfs is therefore unsupported until #115.

This **replaces** the injected `kdump_check` seam (ADR-0030 §4 routed it through the same
`readiness`-style seam): the prerequisite is now deterministic host I/O on a path `install()`
already holds, so an injected `live_vm` seam is unnecessary indirection. The seam, its
`_real_kdump_check` stub, and the `KdumpCheck` type are removed (replace, don't deprecate).

## 6. Where the poll clock lives

`boot()._await_ready` already polls `readiness(system_id)` up to `boot_window_polls` times and
accumulates whether the System ever answered, with **no inter-poll sleep** — so the unit tests
inject a fast fake and run without a real wait. To keep that contract unchanged, `_real_readiness`
is a single per-poll probe whose **own** cadence lives inside the `live_vm` wrapper (the
"injected poll clock" the module docstring names): on a `pending` verdict it checks liveness and,
if the guest is still running, sleeps one poll interval before returning `answered=False`.

**The window is two pinned constants, co-located in `install.py` so neither drifts unseen.** The
effective boot window is `boot_window_polls × _POLL_INTERVAL_SECONDS`. With the existing default
`_DEFAULT_BOOT_WINDOW_POLLS = 30` and `_POLL_INTERVAL_SECONDS = 5`, the window is **150 s** —
chosen with margin over a cold boot of a freshly-built `~/src/linux` kernel through the rootfs
to the `kdive-ready` userspace marker (tens of seconds on the demo host). Both constants live
next to each other with a comment stating their product *is* the window; the `live_vm` acceptance
(§7) is the falsifiable check that 150 s clears the real fixed boot (resolving `ok`, not a
false `BOOT_TIMEOUT`). If the demo host is slower, the interval — not the count — is the tuning
knob. The probe is read-only and stateless across calls, so it holds no cross-poll deadline; the
count×interval product is the only window, and it is the same whether the seam is hit 30 times or
returns terminally on the first. Each poll re-reads and re-classifies the **whole** truncated
log (via the existing `read_console_log`) rather than tailing from a byte offset — intentional:
a per-boot log over a 150 s window is small, statelessness keeps the seam a pure probe, and
correctness (find any pre-marker signature) outweighs re-scan cost on the `live_vm`-only path.
v1's `start_position`/bounded-snippet was for cross-reset reused logs, which the truncate
precondition (§8) makes moot here.

Liveness uses `virsh domstate` (ported from v1's `_domain_is_running`), and the **exit verdict is
guarded exactly as v1 guarded it** — an early `domstate` blip must not become a spurious
`READINESS_FAILURE`:

- A probe that **errors or times out** is *not* proof the guest stopped → treat as `pending`
  (keep polling), matching v1's "a flaky/slow probe keeps waiting."
- Only an explicit **terminal** state (`shut off`, `crashed`) counts as exited. Any other
  non-`running` state (`paused`, `in shutdown`, `pmsuspended`, the brief just-created state) is
  *not* terminal → `pending`.
- Before declaring exited, the probe **re-reads the console once more** and re-classifies: a
  marker (or crash) that landed just before the guest stopped is honored. Only when the re-read
  still yields `pending` and the state is terminal does the probe return `answered=True,
  ok=False` (→ `READINESS_FAILURE`, matching v1's `exited`).

A guest that never comes up at all stays `pending` every poll (`answered=False` across the whole
window → `BOOT_TIMEOUT`). A panic typically leaves the domain *running* (hung at the panic), so
panic detection rests on the console **string** (§4), not on `domstate` — this is the
discrimination the issue calls out.

The readiness `marker` is the demo's literal `kdive-ready` (ADR-0052), a module constant.
Per-rootfs marker resolution (threading the catalog `readiness_marker` through the seam) needs
the System's resolved profile, which the DB-free, `system_id`-keyed seam cannot reach without a
larger change; it is a follow-up.

## 7. Verification

- **Unit — signature mechanism (host-free):** `classify_console` returns `crashed` for each
  signature in §4, `ready` for the bare marker line alone, `pending` for empty/benign/`DEBUG:`-containing
  bytes and for malformed UTF-8; a crash *before* the marker line → `crashed`, a benign signature
  substring *after* the marker line → `ready` (§4.1). The marker is whole-line: a
  `Starting kdive-ready.service` line **without** the bare `kdive-ready` echo → `pending` (the
  systemd unit-name line is not the signal, §3); the bare-line echo → `ready`.
  `_kdump_capture_present` is `True` only when a staged initrd exists; the kdump gate raises
  `CONFIGURATION_ERROR` for a kdump method with no initrd (incl. `initrd_ref=None`) and proceeds
  with one (the existing kdump tests, rewritten to the initrd-presence contract).
- **Unit — committed console fixtures (host-free, the CI guard against tautology):** two fixture
  files under `tests/providers/local_libvirt/fixtures/` — a **crash** console and a **clean**
  console — drive `classify_console` in a CI test: the crash fixture → `crashed`, the clean
  fixture → `ready`. Each is realistic multi-line `ttyS0` output with the `[ ddddd.dddddd]`
  timestamp prefixes and surrounding benign lines (the clean fixture *includes* both a
  `Starting kdive-ready.service` systemd status line **and** the later bare `kdive-ready` echo —
  locking in the whole-line match — plus a post-marker line containing a signature substring to
  lock in the pre-marker scoping; the crash fixture carries a soft-lockup/RCU-stall header with
  `__d_lookup` in the backtrace). Sourced from a real
  `dhash_entries=1` run where one exists, else a format-faithful representative the operator
  replaces with a captured log via the `live_vm` acceptance. This makes the demo's output shape a
  falsifiable check in the merge gate, not only behind the manual `live_vm` job.
- **Unit — truncate-default regression guard (host-free):** a provisioning-XML test asserting the
  rendered `<serial><log>` element carries no `append='on'` (the rendered XML defaults to
  truncate). This pins the §3/§8 precondition that pre-marker scoping relies on, so a future
  change that enables console-log append fails the gate rather than silently introducing a
  stale-marker false-ok. Lives next to the existing provisioning-XML tests
  (`tests/adversarial/test_provider_xml.py`).
- **`live_vm` acceptance (operator-run, gated, ground truth):** fill `test_live_vm_real_install_boot`
  to boot `~/src/linux` @ 7.0 — `dhash_entries=1` resolves to `READINESS_FAILURE` with the
  `__d_lookup` snippet present in the recorded console artifact; the fixed kernel (or without the
  param) resolves `ok` at the `kdive-ready` marker within the §6 window. This is the authoritative
  check that the §4 signatures catch the demo's **real** output and that the §6 window clears a
  real fixed boot; a mismatch is fixed by adding the signature and/or refreshing the fixtures.

## 8. Risks & limitations

- **Console-log truncation is a load-bearing precondition.** The verdict logic (pre-marker
  scoping, §3) is sound because the console log is truncated on each `create()`: QEMU/libvirt
  default chardev `logappend` to off and `provisioning.py` sets no `append` attribute on the
  `<log>` element, so `boot()`'s destroy→create starts every boot from an empty log — no stale
  marker. The risk is therefore not "the log might append" (it does not, by default) but a
  *future regression* that sets `append='on'`: that would both falsely satisfy `ready` from a
  prior marker and, under pre-marker scoping, hide a later crash in the unscanned post-marker
  region (a false-ok on the vulnerable boot). The §7 provisioning-XML guard pins the truncate
  default so the precondition cannot silently regress. v1's `start_position` offset is the
  alternative if append is ever required; it would need `boot()` to thread the pre-power-cycle
  offset into the seam — out of scope while the truncate default holds.
- **Signature completeness.** The §4 set is best-effort; the §7 committed fixtures guard the
  common forms in CI and the `live_vm` acceptance is the ground-truth guard.
- **Boot-window calibration.** The 150 s window (§6) is sized for the demo host; a materially
  slower host needs the interval raised, validated by the `live_vm` acceptance resolving `ok`.
- **Pre-marker false-positive residual.** A non-fatal signature substring emitted *before* the
  marker on a healthy boot still resolves `crashed` (§4.1) — accepted as the fail-closed cost of
  crash-wins; the clean fixture guards the common post-marker forms but cannot cover a benign
  pre-marker splat.
- **kdump check is presence-only** (§5) and rejects embedded-initramfs kdump kernels; full
  in-guest verification regardless of initramfs delivery is #115.
