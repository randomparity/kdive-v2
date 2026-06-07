# ADR 0056 — Live demo cmdline wiring + dcache A/B driver (G5)

- **Status:** Superseded by [ADR-0061](0061-boot-cmdline-composition.md) (the build-ledger
  cmdline source stands; the *replace* semantics here become *append*).
- **Date:** 2026-06-06
- **Issue:** [#128](https://github.com/randomparity/kdive/issues/128) (gap **G5** of
  [#123](https://github.com/randomparity/kdive/issues/123)).
- **Depends on:** [ADR-0030](0030-install-boot-plane.md) (the `Installer`/`Booter` ports and
  the `install`/`boot` ledger steps `_cmdline_for` feeds), [ADR-0029](0029-build-plane.md) (the
  server-build lane and the `(run_id, "build")` ledger row this writes the cmdline into),
  [ADR-0048](0048-external-build-artifact-ingestion.md) (the external lane that already records
  `result["cmdline"]` in that same ledger row — the precedent unified here),
  [ADR-0051](0051-install-method-conditional-crashkernel.md) (`_cmdline_for`, `_install_method_for`,
  the `crashkernel ⇔ kdump` resolution this preserves),
  [ADR-0052](0052-bootable-rootfs-image-builder.md) / [ADR-0053](0053-build-checkout-seam.md) /
  [ADR-0054](0054-object-store-unconditional-read.md) / [ADR-0055](0055-install-readiness-kdump-seam.md)
  (the G1–G4 seams the demo drives).
- **Spec:** [`../superpowers/specs/2026-06-06-dcache-demo-driver-design.md`](../superpowers/specs/2026-06-06-dcache-demo-driver-design.md)

## Context

G1–G4 made the build → install → boot → verify seams real, but the demo cannot yet reproduce
the dcache bug: the kernel command line is **inert**. `_cmdline_for(run, method)` reads
`run.build_profile["cmdline"]`, but no tool path can put it there for the server lane — a
`cmdline` key in `build_profile` fails `runs.build`'s `BuildProfile.parse` (`extra="forbid"`,
ADR-0029 §3). The external lane (`runs.complete_build`) instead records `cmdline` in the
`(run_id, "build")` ledger `result`, but `_cmdline_for` does not read the ledger — the in-code
comment names it "recorded but inert until that wiring lands" (`runs.py`). So today every install
boots the method default (`console=ttyS0[ crashkernel=…]`) and `dhash_entries=1` never reaches
boot. G5 — "the glue that turns the demo on" — is where that wiring lands, plus the demo profiles,
host-staging documentation, and an end-to-end driver. These decisions are settled here so reviews
do not re-litigate them.

The driving test case is [`docs/test-cases/05-dcache-dhash-entries-oob-read.md`](../test-cases/05-dcache-dhash-entries-oob-read.md):
boot `~/src/linux` @ v7.0 with `dhash_entries=1` → OOB read in `__d_lookup()` (no `kdive-ready`);
apply the 7.0.1 fix → the same cmdline boots clean to `kdive-ready`.

## Decisions

### 1. The cmdline's source of record is the build ledger, for both lanes

`runs.build` gains an optional `cmdline: str | None` parameter. It is threaded through the build
job payload into `build_handler`, which writes it into the `(run_id, "build")` ledger `result`
(`result["cmdline"]`) at finalize — exactly where the external lane (`complete_build`) already
records it. There is now **one** cmdline source of record (the build ledger), not a per-lane split.
A blank/absent value records no cmdline and the method default applies (unchanged behavior for
every caller that omits it).

### 2. `_cmdline_for` reads the ledger; the `build_profile` read is removed (replace, don't deprecate)

`_cmdline_for` becomes `async _cmdline_for(conn, run, method) -> str`: it reads the `(run_id,
"build")` ledger `result["cmdline"]`; a non-blank string is the cmdline, otherwise the
method-appropriate default (`_KDUMP_DEFAULT_CMDLINE` / `_NONKDUMP_DEFAULT_CMDLINE`, ADR-0051 §3).
The prior `run.build_profile.get("cmdline")` read is **removed**, not kept as a fallback: no tool
path ever populated it (the build parse rejects the key), so it was unreachable except by a
hand-constructed `Run` in a unit test. Keeping it would be a dead second source. The two call
sites — the `install_run` admission gate (the kdump `crashkernel=` token check) and
`install_handler` — both already hold a `conn`, so the signature change is local. This single
change activates the cmdline for **both** lanes, closing the documented gap for the external lane
too.

### 3. The demo profiles resolve from operator env; nothing kernel-sized is checked in

The demo's build inputs and rootfs are operator-provided through the env seams G1/G3 already
established — `KDIVE_KERNEL_SRC` (the warm `~/src/linux` tree), `KDIVE_TEST_BUILD_CONFIG` (a
`.config` that satisfies the `CONFIG_CRASH_DUMP` + DWARF/BTF preflight, ADR-0029), `KDIVE_GUEST_IMAGE`
(the G3 rootfs qcow2) — plus one new env, `KDIVE_DEMO_FIX_PATCH` (a `git apply -p1`-able patch that
carries the dcache fix). A small reusable helper builds the two build profiles (vulnerable =
no `patch_ref`; fixed = `patch_ref` from the env) and the provisioning profile from these vars; it
is unit-tested host-free against `BuildProfile.parse` / `ProvisioningProfile.parse`. No full
`.config` or synthetic patch is committed — the runbook documents generating each. This matches the
existing `live_vm` env-seam convention (`test_live_vm_real_make_build_id_matches_readelf`,
`test_live_stack._provision_profile`).

### 4. The provisioning profile is console-only; no libvirt network, no destructive opt-in

The dcache case is observed on the **console** (the crash is a pre-userspace `__d_lookup` fault,
ADR-0055 §3), not over SSH and not via `force_crash`. So the demo provisioning profile carries
**no** `crashkernel` (→ `_install_method_for` resolves `CONSOLE`, ADR-0051 §1), **no** SSH
credential, and **no** `destructive_ops`. No libvirt network is required (SSH readiness is the only
thing that needs one). The demo cmdline is the literal `console=ttyS0 dhash_entries=1`.

### 5. A/B topology: one System, two sequential Runs

The demo provisions **one** System and runs two Runs against it in sequence: Run A (vulnerable,
no patch) build → install → boot → **crash on console**; Run B (fixed, `patch_ref`) build → install
→ reboot → **ready**. A console-only crash does not transition the System (only `force_crash` drives
`ready → crashed`, and boot never changes System state in M0), so the System stays `ready`
(`_RUN_HOSTABLE`) and hosts Run B. This is the topology `boot_handler` already anticipates — "a
re-boot of the same System (a new Run)", "M0 boots a System's Runs sequentially", and the
System-scoped console object overwritten with a fresh etag per boot (the #117 etag-refresh). It
proves the fix on the same box with the same trigger — the meaningful A/B — and exercises the
reinstall/reboot path a two-System layout would not.

### 6. The driver is a `live_vm` handler-level test; CI coverage is the host-free profile + wiring tests

The end-to-end driver is `@pytest.mark.live_vm`, driving the build/install/boot **handlers** with
the real providers (the repo's prescribed test boundary, ADR-0019 — handlers, not the HTTP
transport), gated by the established `_live_vm_preflight` so it skips cleanly in CI and on any host
without the fixtures. It does not stand up the `server`/`worker`/`reconciler` processes the
`live_stack` spine needs — a console-only single-System A/B does not need the wire. What runs in CI
is non-gated: the demo-profile builders parse correctly and carry the right cmdline / patch / method,
and the cmdline-wiring unit tests (ledger-sourced override beats default; default when absent). The
ground-truth A/B is the `live_vm` job + the runbook.

### 7. Host staging and the one-command path are documented in a runbook

A new `docs/runbooks/dcache-demo.md` is the acceptance artifact: it documents making
`/var/lib/kdive/{rootfs,install,build,console}` worker-writable **or** pointing
`KDIVE_BUILD_WORKSPACE` / `KDIVE_INSTALL_STAGING` / the rootfs+console dirs at writable paths,
generating the `.config` and fix patch, and the one command that reproduces test-case 05
(`just test-live` scoped to the demo). It builds on `docs/runbooks/live-stack.md` rather than
duplicating the stack bring-up.

## Consequences

- `dhash_entries=1` reaches boot: the vulnerable kernel's console classifies `crashed`
  (`__d_lookup`, ADR-0055) and its boot fails — `readiness_failure` when the crash signature is
  seen in the pre-marker region, `boot_timeout` if it hangs without one; the fixed kernel boots
  `ok` to `kdive-ready`. The console classification is the ground-truth signal, not the category.
- The external lane's previously-inert `complete_build(cmdline=…)` is now live too (Decision 2);
  `docs/guide/reference/runs.md`'s "inert until that wiring lands" note is corrected, and the
  agent-facing tool guide / its snapshot are regenerated for the new `runs.build` `cmdline` param.
- `_cmdline_for` is now `async` and DB-reading; the three `_cmdline_for` unit tests are rewritten
  from the removed `build_profile` source to the ledger source (the behavior — override beats
  default — is preserved; the source of record changed by design).
- The demo is reproducible from a documented runbook but is **not** exercised end-to-end in CI (it
  needs the libvirt host + a multi-minute kernel build); CI retains a falsifiable signal through the
  host-free profile and wiring tests, and ADR-0055's committed console fixtures.

## Considered & rejected

- **A `cmdline` parameter on `runs.install`** (store it on the install step). Matches the issue's
  "overrides at `runs.install`" phrasing literally, but the cmdline source would then differ by lane
  — external records at build (`complete_build`), server at install — two records to keep
  consistent. Rejected for a single source of record at the build ledger (Decision 1).
- **Let `runs.create`'s `build_profile` carry `cmdline`** (strip it before `BuildProfile.parse`).
  Simplest call site, but it puts a boot concept into the immutable build profile — the exact
  layering the codebase already cites for refusing it (ADR-0003/0011, `extra="forbid"`). Rejected
  (Decision 1).
- **Keep the `build_profile["cmdline"]` read as a fallback.** A second, unreachable cmdline source
  alongside the ledger; "replace, don't deprecate." Rejected (Decision 2).
- **Two Systems (one per kernel).** Simpler state-wise, but does not prove the fix on the same box
  and skips the reinstall/reboot path; spends double the host resources. Rejected for one System,
  two Runs (Decision 5).
- **A `live_stack` HTTP spine driver for the demo.** Closest to "builds on the live-stack runbook",
  but a console-only single-System A/B does not need the wire, and it would require the full stack
  (compose backends + three host processes) to run at all. Rejected for the lighter `live_vm`
  handler-level driver (Decision 6); the `live_stack` spine (`test_live_stack.py`) remains the
  full-wire exercise.
- **Runbook + shell script only, no gated test.** Lowest code surface, but the demo would be
  encoded only in prose and could rot with no executable check. Rejected for a `live_vm` driver
  plus the runbook (Decisions 6–7).
- **Commit a `.config` fragment and the fix patch as repo fixtures.** Self-contained, but a usable
  `.config` is kernel-sized and the synthetic 7.0→7.0.1 patch must be authored and maintained in the
  tree. Rejected for operator-supplied env, documented in the runbook (Decision 3).
