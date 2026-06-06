# ADR 0055 — Install readiness + kdump-check seam: console classifier, host initrd gate (G4)

- **Status:** Proposed
- **Date:** 2026-06-06
- **Issue:** [#127](https://github.com/randomparity/kdive/issues/127) (gap **G4** of
  [#123](https://github.com/randomparity/kdive/issues/123)).
- **Depends on:** [ADR-0030](0030-install-boot-plane.md) (the `Booter`/`Installer` ports, the
  injected readiness/kdump seams, and the `boot_timeout` vs `readiness_failure` split this
  fills), [ADR-0049](0049-crash-capture-tiers.md) (the always-on console tier; the deferral of
  in-guest kdump to #115), [ADR-0052](0052-bootable-rootfs-image-builder.md) (the `kdive-ready`
  console marker).
- **Precedent:** [ADR-0054](0054-object-store-unconditional-read.md) (the G2 fetch seam's
  unit-tested pure core under a thin `live_vm` wrapper — the shape this reuses).
- **Spec:** [`../superpowers/specs/2026-06-06-install-readiness-kdump-seam-design.md`](../superpowers/specs/2026-06-06-install-readiness-kdump-seam-design.md)

## Context

`install.py`'s `_real_readiness` and `_real_kdump_check` both `raise MISSING_DEPENDENCY`.
`boot()` already polls the readiness seam and maps `ReadinessResult(answered, ok)` to
`boot_timeout`/`readiness_failure`, but has no real probe — so there is no "verify the bug
occurred" signal for the live demo (#123). A vulnerable boot (`dhash_entries=1`) must resolve
to **failure** and the fixed kernel to **ok**. v1 realized this with
`SubprocessLibvirtRunner.stream_console` (tail the teed console for a marker, poll `virsh
domstate` for an early exit, bounded snippet). These decisions are settled here so reviews do
not re-litigate them.

## Decisions

### 1. The readiness verdict is a pure classifier over console bytes

`classify_console(data: bytes, *, marker: str) -> Literal["ready", "crashed", "pending"]` is a
host-free pure function — the unit-testable core, mirroring the G2 fetch seam's `_stage_object`
under `_real_fetch` (ADR-0054). It decodes `utf-8`/`errors="replace"` (a partial or non-UTF-8
tail never raises) and returns `crashed` on a crash signature, else `ready` on the marker, else
`pending`. `_real_readiness` is the thin `live_vm` wrapper that reads the log, polls liveness,
and maps the verdict to `ReadinessResult`.

### 2. The verdict carries no console text; redaction stays at the existing boundary

The seam returns only `ReadinessResult(answered, ok)` — booleans, no guest output. No untrusted
text crosses the seam, so no redaction is added here. The crash **snippet** the acceptance names
is captured and redacted by the `runs.boot` handler's console-artifact registration (ADR-0049
§4), which already runs on every boot outcome independent of this verdict.

### 3. Crash wins over the marker (fail-closed precedence)

When both a crash signature and the marker are present, the verdict is `crashed`. The seam's
purpose is a trustworthy verification signal; a false `ok` would defeat it. The demo's vulnerable
boot crashes before the marker is emitted, so this only governs the pathological both-present
case (e.g. a stale marker in a reused log).

### 4. Crash signatures are a fail-closed, additive set

A fatal/stall-grade substring set (`Kernel panic`, `BUG:` with a `DEBUG:`-excluding lookbehind,
`Oops:`, `general protection fault`, `[Uu]nable to handle kernel`, `KASAN:`, `KFENCE:`,
`detected stall`). Non-exhaustive by design: a missed signature is a one-line addition, not a
contract change. Unit tests pin the mechanism; the `live_vm` acceptance is the falsifiable check
that the demo's real `dhash_entries=1` output is caught.

### 5. kdump-check is a host-observable initrd-presence gate, not an in-guest probe

A real kdump preflight is irreducibly in-guest (v1's `kdump_probe.py` execs a probe script in
the booted guest), needing the Tier-3 kdump guest image and a guest transport both deferred to
#115 (ADR-0049 §6). The one prerequisite checkable on the host at install time is a staged
**initrd** — the capture environment's carrier, without which `crashkernel=` is inert (ADR-0030
§4). So `install()` gates a `method == KDUMP` install on `_kdump_capture_present(initrd_path)`
(`initrd_path is not None and initrd_path.exists()`), raising `configuration_error` otherwise.
This is **necessary but not sufficient** and is documented as such; the deeper guest-side
verification is #115.

### 6. The injected `kdump_check` seam is removed, not kept

The prerequisite is now deterministic host I/O on a path `install()` already holds, so the
injected `KdumpCheck`/`readiness`-style seam (ADR-0030 §4) is unnecessary indirection. The
`kdump_check` constructor parameter, the `KdumpCheck` type, and the `_real_kdump_check` stub are
removed (replace, don't deprecate). The `readiness` seam stays injected — it is genuinely
`live_vm` (it tails a file and execs `virsh`).

### 7. The poll clock lives inside the `live_vm` readiness seam

`boot()._await_ready` polls the readiness seam `boot_window_polls` times with no inter-poll
sleep (so unit tests run without a real wait). `_real_readiness` therefore owns its own cadence:
on `pending`, if `virsh domstate` shows the guest still running, it sleeps one poll interval
before returning `answered=False`; the total window is `boot_window_polls × interval`, entirely
inside the `live_vm`-gated wrapper. `_await_ready` and the unit tests are untouched. An exited
guest that never reached the marker is `answered=True, ok=False` (→ `readiness_failure`, v1's
`exited`); a guest that never comes up is `answered=False` throughout (→ `boot_timeout`).

## Consequences

- The demo's verification signal is real: `dhash_entries=1` → `readiness_failure` with the
  `__d_lookup` snippet in the console artifact; the fixed kernel → `ok` at `kdive-ready`.
- `LocalLibvirtInstall.__init__` and `from_env` lose the `kdump_check` parameter; the two
  existing kdump unit tests are rewritten to the initrd-presence contract.
- The readiness marker is the literal `kdive-ready` (a module constant). Per-rootfs marker
  resolution (the catalog `readiness_marker`) needs the System's resolved profile, which the
  DB-free `system_id`-keyed seam cannot reach — a follow-up.
- A stale `kdive-ready` in a libvirt log that **appends** across re-boots could produce a false
  `ok` for a prior-success → later-silent-hang sequence. Crash-wins precedence keeps the
  verification signal sound; the v1 `start_position` offset fix needs a `boot()`/seam-signature
  change and is a tracked follow-up. Whether the libvirt `<log file>` tee truncates or appends on
  domain start is a dependency to verify, not assumed.
- kdump remains presence-only until #115 wires the in-guest probe; `local-libvirt` still does not
  advertise `kdump` in its supported-set (ADR-0049 §2).

## Considered & rejected

- **Block readiness as a single blocking `stream_console` call (the literal v1 shape).** v1's
  runner blocks for the whole window in one call. The rewrite's `boot()` already owns a polling
  loop over a per-poll seam; making the seam block the full window would make 29 of 30 loop
  iterations redundant or, worse, multiply the window. Rejected for a per-poll probe that fits
  the existing loop (Decision 7).
- **Add an injected `sleep` clock to `_await_ready`.** Threading a sleep seam through `boot()`
  changes the orchestration and the unit-test construction for a cadence that only the `live_vm`
  path needs. Rejected for keeping the clock inside the `live_vm` seam (Decision 7).
- **Marker wins over crash.** A boot that printed the marker is "ready" even if it later crashed.
  Rejected: the seam is a verification signal; fail-closed (crash-wins) is the safe default
  (Decision 3).
- **Use `domstate` as the primary crash signal.** A panicked kernel hangs with the domain still
  *running*, so `domstate` cannot detect a panic; the console string is the only reliable signal.
  `domstate` is retained only to distinguish an early *exit* from a still-booting guest
  (Decision 7).
- **Return the console snippet from the readiness seam.** Would put untrusted guest text on the
  seam boundary and duplicate the `runs.boot` handler's redacted console artifact. Rejected:
  booleans only (Decision 2).
- **Implement the full in-guest kdump probe now (port v1 `kdump_probe.py`).** Needs the deferred
  Tier-3 guest image and a guest transport M0 lacks; the verdict core would be dead code until
  #115. Rejected as speculative for a host-observable presence gate (Decision 5).
- **Keep `_real_kdump_check` as a deferred `live_vm` stub.** Leaves a `MISSING_DEPENDENCY` stub
  reachable by a `crashkernel`-provisioned System. Rejected for a real, host-free gate now
  (Decisions 5–6).
