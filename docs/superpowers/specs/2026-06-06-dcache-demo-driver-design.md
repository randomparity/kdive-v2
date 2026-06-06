# Design — Live demo cmdline wiring + dcache A/B driver (G5, #128)

- **Issue:** [#128](https://github.com/randomparity/kdive/issues/128) (gap **G5** of the
  live-seam demo epic [#123](https://github.com/randomparity/kdive/issues/123)).
- **ADR:** [ADR-0056](../../adr/0056-live-demo-cmdline-wiring-dcache-driver.md) — the settled
  decisions and rejected alternatives.
- **Driving test case:** [`docs/test-cases/05-dcache-dhash-entries-oob-read.md`](../../test-cases/05-dcache-dhash-entries-oob-read.md).

## Goal

Make the dcache `dhash_entries=1` demo reproducible end-to-end on the real libvirt host, building
on the G1–G4 seams: an agent (or operator) builds `~/src/linux` @ v7.0, boots it with
`dhash_entries=1`, observes the `__d_lookup()` out-of-bounds read on the console, applies the
7.0.1 fix patch, rebuilds, reboots with the same cmdline, and confirms the kernel now boots clean
to `kdive-ready`.

## Background — why the cmdline is the blocker

The orchestration is already real (G1 checkout, G2 fetch, G3 rootfs, G4 readiness classifier), but
the kernel command line never reaches boot:

- `_cmdline_for(run, method)` (`mcp/tools/runs.py`) reads `run.build_profile["cmdline"]`.
- The server lane cannot put it there: `runs.create` stores `build_profile` verbatim, but
  `runs.build` then calls `BuildProfile.parse(run.build_profile)`, which is `extra="forbid"` — a
  `cmdline` key fails the build with `configuration_error`.
- The external lane (`runs.complete_build`) records `cmdline` in the `(run_id, "build")` ledger
  `result` instead (immutable-profile reason), but `_cmdline_for` does not read the ledger.

So `_cmdline_for`'s `build_profile` read is reachable only from a hand-constructed `Run` in a unit
test; via the tools, every install boots the method default and `dhash_entries=1` is inert. The
in-code comments say exactly this ("recorded but inert until that wiring lands"). G5 lands the
wiring.

## Design

### Part A — cmdline wiring (the glue)

Source of record: the `(run_id, "build")` ledger `result["cmdline"]`, for **both** lanes
(ADR-0056 §1–2).

1. `build_run(pool, ctx, run_id, *, cmdline: str | None = None)` — new optional keyword. The MCP
   wrapper exposes it with a description mirroring `complete_build`'s `cmdline`
   (`e.g. 'console=ttyS0 dhash_entries=1'`).
2. The build job payload carries the cmdline: `_enqueue_build` writes
   `{"run_id": …, "cmdline": cmdline}` when `cmdline` is a non-blank string (omitted otherwise, so
   existing dedup-key and payload shapes are unchanged for the default path).
3. `build_handler` reads `job.payload.get("cmdline")` and, when present, sets
   `result["cmdline"]` before `_finalize_build` writes the ledger row. A rebuild that finds an
   existing ledger row keeps that row (the existing `_existing_build_result` short-circuit) —
   idempotent.
4. `_cmdline_for` becomes `async _cmdline_for(conn, run, method) -> str`: read
   `_existing_build_result(conn, run.id)`; `result["cmdline"]` (non-blank `str`) wins, else the
   method default. The `build_profile` read is removed (ADR-0056 §2).
5. Both call sites — the `install_run` admission gate and `install_handler` — `await` the new
   signature; both already hold a `conn`.

Behavior matrix (`_cmdline_for`):

| ledger `result["cmdline"]` | method | result |
|---|---|---|
| `"console=ttyS0 dhash_entries=1"` | CONSOLE | `"console=ttyS0 dhash_entries=1"` |
| absent / blank / non-str | KDUMP | `_KDUMP_DEFAULT_CMDLINE` (`console=ttyS0 crashkernel=256M`) |
| absent / blank / non-str | CONSOLE | `_NONKDUMP_DEFAULT_CMDLINE` (`console=ttyS0`) |
| no build ledger row yet | any | method default |

The kdump `crashkernel=` admission check in `install_run` (ADR-0051 §3) is unchanged: it still runs
against the resolved cmdline, now ledger-sourced.

### Part B — demo profiles (host-free helper)

A reusable helper (consumed by the driver and its host-free unit test) builds the demo's profiles
from env (ADR-0056 §3–4):

- `demo_build_profile(*, fixed: bool)` → `{schema_version: 1, kernel_source_ref:
  $KDIVE_KERNEL_SRC, config_ref: $KDIVE_TEST_BUILD_CONFIG[, patch_ref: $KDIVE_DEMO_FIX_PATCH]}`.
  `fixed=False` omits `patch_ref` (vulnerable); `fixed=True` includes it.
- `demo_provisioning_profile()` → console-only: `{schema_version: 1, arch: "x86_64", vcpu, memory_mb,
  disk_gb, boot_method: "direct-kernel", kernel_source_ref, provider: {"local-libvirt": {rootfs:
  {kind: "path", path: $KDIVE_GUEST_IMAGE}}}}` — no `crashkernel`, no SSH, no `destructive_ops`.
- `DEMO_CMDLINE = "console=ttyS0 dhash_entries=1"`.
- A preflight that resolves the four env vars (`KDIVE_KERNEL_SRC`, `KDIVE_TEST_BUILD_CONFIG`,
  `KDIVE_GUEST_IMAGE`, `KDIVE_DEMO_FIX_PATCH`) or `pytest.skip`s with the exact fix, in the style of
  `_live_vm_preflight`.

### Part C — the `live_vm` A/B driver

`@pytest.mark.live_vm` (`# pragma: no cover - live_vm`), driving the handlers with real providers
(ADR-0056 §5–6). One System, two sequential Runs:

1. Preflight (skip cleanly if any of the four env vars is unset/missing).
2. Seed a granted allocation; provision **one** System with `demo_provisioning_profile()` via the
   real provisioning handler; await `ready`.
3. Open an investigation. **Run A (vulnerable):** `create_run(build_profile=demo_build_profile(fixed=False))`
   → `build_run(cmdline=DEMO_CMDLINE)` + `build_handler` (real `LocalLibvirtBuild`) →
   `install_run` + `install_handler` (real `Installer`) → `boot_run` + `boot_handler` (real
   `Booter`). Capture and assert Run A's evidence **before** Run B's install/boot overwrites it:
   the registered console artifact classifies as `crashed` with `__d_lookup` in the captured text,
   and that same console shows `dhash_entries=1` in its `Command line:` line (so a clean boot from a
   *missing* cmdline is distinguished from a fixed kernel — see the false-negative edge below). The
   boot itself fails — `readiness_failure` if the crash signature is seen, `boot_timeout` if the
   guest hangs without one.
4. **Run B (fixed):** a new Run on the **same** System: `create_run(build_profile=
   demo_build_profile(fixed=True))` → `build_run(cmdline=DEMO_CMDLINE)` → install → boot. Assert the
   boot **succeeds** and the (overwritten) console artifact classifies as `ready`.

The `crashed`/`ready` console classification is the ground-truth assertion (ADR-0055's
`classify_console`); the boot-job outcome (`readiness_failure`/`boot_timeout` vs success) is the
secondary assertion.

Three dependencies the driver rests on, made explicit so a reorder cannot quietly void the A/B:

- **Install selects the kernel, not boot.** `boot(system_id)` only power-cycles the domain;
  `install()` is what redefines the domain XML to the per-Run staged kernel
  (`{staging_root}/{system_id}/{run_id}/kernel`, `install.py` `defineXML`). So Run B boots the fixed
  kernel only because Run B's `install_handler` redefines the shared domain *before* Run B's boot;
  the per-Run staging path keeps A's and B's kernels distinct on disk.
- **Console capture before overwrite.** The console object and the host log are System-scoped and
  truncated/overwritten on each `create()` (the G4 precondition; `boot_handler` etag-refresh). Run
  A's `crashed` evidence must be read and asserted before Run B boots; the A and B verdicts are not
  both recoverable from the System-scoped console after the run finishes.
- **A/B differs only by the patch.** Both Runs use the same `$KDIVE_TEST_BUILD_CONFIG` and the same
  `DEMO_CMDLINE`; the only difference is Run B's `patch_ref`. This is the invariant that makes Run
  B's `ready` verdict attributable to the dcache fix rather than to config or cmdline drift.

### Part D — host staging + runbook (the acceptance artifact)

`docs/runbooks/dcache-demo.md` documents, in order:

1. Prerequisites (the #123 host; the G3 rootfs via `scripts/live-vm/build-guest-image.sh`).
2. Worker-writable staging: either `chown`/`chmod` `/var/lib/kdive/{rootfs,install,build,console}`
   for the worker, **or** export `KDIVE_BUILD_WORKSPACE` / `KDIVE_INSTALL_STAGING` and the rootfs /
   console dirs to writable paths. No libvirt network (console-only).
3. Generating `KDIVE_TEST_BUILD_CONFIG` (`make defconfig`, then enable `CONFIG_CRASH_DUMP` and one
   of `CONFIG_DEBUG_INFO_DWARF5` / `…DWARF4` / `…BTF`) and `KDIVE_DEMO_FIX_PATCH` (the dcache fix as
   a `-p1` patch). The runbook names the fix's provenance (the 7.0.1 dcache change) and tells the
   operator to verify the patch applies with `git apply --check` against the v7.0 tree before the
   run — a patch that does not apply fails Run B at *build* with `configuration_error` (G1), not at
   boot.
4. The one command that reproduces test-case 05 (`just test-live` scoped to the demo), and the
   agent-facing tool-by-tool walkthrough (allocate → provision → create-run → build(cmdline) →
   install → boot, twice).

## Success criteria (falsifiable)

- **Wiring (CI):** with a ledger `result["cmdline"] = "console=ttyS0 dhash_entries=1"`,
  `_cmdline_for` returns it for any method; with no/blank ledger cmdline it returns the
  method-appropriate default. `build_run(cmdline=…)` causes `build_handler` to write
  `result["cmdline"]`; omitting it writes none.
- **Profiles (CI):** `demo_build_profile(fixed=False/True)` parse as `ServerBuildProfile` (no
  patch / with patch); `demo_provisioning_profile()` parses as `ProvisioningProfile` and resolves
  to `CaptureMethod.CONSOLE` via `_install_method_for`.
- **Skip (CI / any host):** the driver and the preflight skip cleanly with an actionable reason
  when any of the four env vars is absent.
- **End-to-end (`live_vm`, manual):** on the real host, Run A's console classifies `crashed`
  (`__d_lookup`) and shows `dhash_entries=1` in its `Command line:` line, and its boot fails
  (`readiness_failure` if the signature is seen, else `boot_timeout`); Run B (fixed, same System,
  same config + cmdline, only `patch_ref` added) classifies `ready` and boots clean. The runbook's
  one command drives this. **Dependency:** crash detection relies on the real `dhash_entries=1`
  output carrying a recognized crash signature in the pre-marker region; if a real capture hangs
  without one, the fix is a one-line signature addition (ADR-0055 §4), not a driver change.

## Edges and failure modes

- **Blank/whitespace cmdline** → treated as absent (method default), matching the existing
  `value.strip()` guard.
- **`build_run(cmdline=…)` on an external-source Run** is rejected before enqueue by the existing
  `parsed.source != "server"` gate — the cmdline param does not change that path (the external lane
  sets cmdline via `complete_build`).
- **kdump + ledger cmdline without `crashkernel=`** → `install_run` returns
  `configuration_error` `cmdline_missing_crashkernel` (unchanged gate, now over the ledger source).
- **No build ledger cmdline → silent false-negative A/B.** If `runs.build` is called without
  `cmdline=` (or before the build finalize commits), `_cmdline_for` returns the method default
  (`console=ttyS0`, no `dhash_entries=1`) and the "vulnerable" kernel boots clean — the bug never
  triggers and the A/B is vacuous. This is the most likely operator mistake in the runbook's
  tool-by-tool path. Mitigations: the driver asserts Run A's console shows `dhash_entries=1`
  (Part C) so a missing cmdline is distinguished from a fixed kernel, `install_handler` logs the
  resolved cmdline, and the runbook flags the `cmdline=` argument as load-bearing. (The
  `runs.boot` "install first" / build-succeeded gates still order the steps; the driver `jobs.wait`s
  build before install.)
- **`KDIVE_DEMO_FIX_PATCH` does not apply** (stale tree, fuzz, CRLF) → G1's `_apply_patch` maps it
  to `configuration_error` at Run B's *build*, not its boot. The runbook pre-checks with
  `git apply --check`.
- **Vulnerable boot leaves the System bootable for Run B** — a console crash does not flip System
  state; verified by the topology decision (ADR-0056 §5).

## Out of scope

- In-guest kdump verification (#115) — the demo is console-only.
- The pre-built-image catalog direction (the recorded long-term rootfs goal).
- Containerizing the host processes / a `live_stack` form of the demo.
- Any change to the readiness classifier, the fetch seam, or the checkout seam (G2–G4 are done).

## Test plan

- `tests/.../test_dcache_demo_profiles.py` (host-free): profile shapes + method resolution + the
  preflight skip.
- `tests/mcp/test_runs_tools.py`: the rewritten `_cmdline_for` ledger-source tests + a
  `build_run(cmdline=…)`-records-the-ledger test (against the disposable Postgres) and the
  default-when-absent path.
- `tests/integration/test_dcache_demo.py`: the `@pytest.mark.live_vm` A/B driver (skips in CI).
- Regenerate the agent-facing tool guide snapshot for the new `runs.build` `cmdline` param and
  correct `docs/guide/reference/runs.md`.
