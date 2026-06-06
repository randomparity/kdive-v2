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
- `tests/providers/local_libvirt/test_install.py` — unit tests for the classifier (every
  signature, the marker, precedence, empty/malformed bytes), the kdump initrd-presence gate,
  and the existing `live_vm` `test_live_vm_real_install_boot` stub filled with the real
  boot-to-verdict acceptance (still `live_vm`-gated).

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

- **`crashed`** — the console contains a kernel crash/stall signature (§4). Resolved
  **first** (crash-wins precedence, §4.1).
- **`ready`** — the console contains the readiness `marker` and no crash signature.
- **`pending`** — neither; the guest is still booting (or the log is not yet written).

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

### 4.1 Precedence: crash wins over marker

When both a crash signature and the marker are present in the scanned console, the verdict is
**`crashed`**. This is fail-closed: the seam exists to produce a trustworthy verification
signal, and a false `ok` would defeat it. In practice the demo's vulnerable boot crashes
*before* the rootfs emits the marker, so the marker is absent; the precedence only governs the
pathological both-present case (e.g. a stale marker from a prior boot in a reused log, §6).

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
if the guest is still running, sleeps one poll interval before returning `answered=False`. The
total boot window is therefore `boot_window_polls × poll interval`, owned entirely by the
`live_vm`-gated seam — `_await_ready` and every unit test are untouched.

Liveness uses `virsh domstate` (ported from v1's `_domain_is_running`): a guest that has
**exited** without reaching the marker is `answered=True, ok=False` (→ `READINESS_FAILURE`,
matching v1's `exited`), distinct from a guest that never comes up at all (`answered=False`
across the whole window → `BOOT_TIMEOUT`). A panic typically leaves the domain *running* (hung
at the panic), so panic detection rests on the console **string** (§4), not on `domstate` —
this is the discrimination the issue calls out.

The readiness `marker` is the demo's literal `kdive-ready` (ADR-0052), a module constant.
Per-rootfs marker resolution (threading the catalog `readiness_marker` through the seam) needs
the System's resolved profile, which the DB-free, `system_id`-keyed seam cannot reach without a
larger change; it is a follow-up.

## 7. Verification

- **Unit (host-free, the prescribed boundary):** `classify_console` returns `crashed` for each
  signature in §4, `ready` for the marker alone, `pending` for empty/benign/`DEBUG:`-containing
  bytes and for malformed UTF-8; crash-wins precedence when both present. `_kdump_capture_present`
  is `True` only when a staged initrd exists; the kdump gate raises `CONFIGURATION_ERROR` for a
  kdump method with no initrd and proceeds with one (the existing kdump tests, rewritten to the
  initrd-presence contract).
- **`live_vm` acceptance (operator-run, gated):** fill `test_live_vm_real_install_boot` to boot
  `~/src/linux` @ 7.0 — `dhash_entries=1` resolves to `READINESS_FAILURE` with the `__d_lookup`
  snippet present in the recorded console artifact; the fixed kernel (or without the param)
  resolves `ok` at the `kdive-ready` marker. This is the falsifiable check that the §4 signature
  set catches the demo's real output.

## 8. Risks & limitations

- **Stale marker in a reused console log.** libvirt's serial `<log file>` tee may *append*
  across a re-boot of the same System rather than truncate (a dependency to verify, not assume —
  ADR-0049 already notes the System-scoped console is overwritten per boot at the artifact
  layer, but the host log file is a separate question). If it appends, a prior boot's
  `kdive-ready` could be read by a later boot that hangs without re-emitting it → a false `ok`.
  Crash-wins precedence keeps the *verification* signal sound (a boot that crashes still
  resolves not-ok despite a stale marker); the residual false-`ok` is only for a prior-success →
  later-silent-hang sequence. The v1 `start_position` offset that solved this needs `boot()` to
  thread the pre-power-cycle byte offset into the seam — a `boot()`/seam-signature change scoped
  out here and tracked as a follow-up.
- **Signature completeness.** The §4 set is best-effort; the live_vm acceptance is the guard.
- **kdump check is presence-only** (§5); full in-guest verification is #115.
