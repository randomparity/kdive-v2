# ADR 0030 — Install + boot plane (local libvirt): runs.install, runs.boot, install/boot handlers (M0)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** #19 (M0: Install + boot plane)
- **Depends on:** [ADR-0029](0029-build-plane-local-make.md) (the realized-port +
  handler pattern, the `run_steps` step ledger, the `(run_id, step)` dedup_key, and the
  `kernel_ref` this plane stages), [ADR-0026](0026-investigation-run-lifecycle.md) (the
  Run lifecycle and the `build → install → boot` step model),
  [ADR-0025](0025-provisioning-plane-libvirt.md) (the libvirt connection-factory seam and
  the System-tagged domain this plane redefines),
  [ADR-0028](0028-control-plane-power-force-crash.md) (the power-cycle libvirt
  operations boot reuses), [ADR-0016](0016-repository-layer-locks-idempotency.md) (the
  advisory locks and the `run_steps` ledger / `run_step` helper),
  [ADR-0013](0013-object-store-layout-retention.md) /
  [ADR-0017](0017-object-store-client-interface.md) (the artifact store the staged
  kernel is fetched from).
- **Refines:** the M0 Install/Boot wording in
  [`../specs/m0-walking-skeleton.md`](../specs/m0-walking-skeleton.md) ("Local-libvirt
  provider → Install", the "kdump prerequisite", "run-readiness preflight → Run
  lifecycle") and the issue-19 scope in
  [`../plans/m0-implementation.md`](../plans/m0-implementation.md).
- **Spec:** [`../superpowers/specs/2026-06-04-install-boot-plane-design.md`](../superpowers/specs/2026-06-04-install-boot-plane-design.md)

## Context

A Run reaches `succeeded` once its kernel is built and `kernel_ref` is recorded
(ADR-0029), but the System still runs whatever it provisioned with. Issue #19 adds the
next two Run steps: `runs.install` stages the built kernel for direct-kernel boot with a
`crashkernel=` reservation and verifies the kdump capture prerequisite, and `runs.boot`
boots the installed kernel and runs the run-readiness preflight before declaring it
ready. The `JobKind.INSTALL`/`BOOT` enums, the `run_steps` ledger, the `install_failure`/
`boot_timeout`/`readiness_failure` categories, the `boot` audit `tool` value, and the
System-tagged libvirt domain all already exist; #19 adds the realized provider ports,
the two tools, and the two handlers.

Several decisions the parent spec and ADR-0026 leave open are settled here.

## Decision

### 1. Install and boot are `run_steps` of one `succeeded` Run — they take no new `RunState`

ADR-0026 §1 models a Run as `build → install → boot`; ADR-0029 §5 drives the Run
`running → succeeded` on the **build** step, and `RunState.SUCCEEDED` is terminal (no
forward edge). The walking skeleton models the three as **idempotent steps keyed
`(run_id, step)`**, with `run_steps(step)` recording each. So install and boot are
recorded **only** in the `run_steps` ledger (generic `step` column — no schema change)
and never re-flip the Run `state`. The Run's `state` tracks the build (the step that
writes the durable `kernel_ref`/`debuginfo_ref` columns); install/boot results live in
the ledger.

By Run state at `runs.install`/`runs.boot`:

| Run state | result |
|-----------|--------|
| `succeeded` | install/boot admitted (the kernel is built) |
| `created` / `running` | `configuration_error` (`data.current_status`) — build the Run first |
| `failed` / `canceled` | `configuration_error` (`data.current_status`) — terminal/dead Run |

`runs.boot` additionally requires a **succeeded** `install` step (`configuration_error`
"install first" otherwise), so the step order `install → boot` is enforced at the tool.

### 2. An install/boot **failure** records no step row; the dead-lettered job carries the category, and the Run is not driven `failed`

`RunState` has no `succeeded → failed` edge, and the Run's build genuinely succeeded —
so an install or boot failure does **not** touch the Run's `state`. It also records **no
`run_steps` row**: `run_step` (`src/kdive/db/idempotency.py`) writes a `state='succeeded'`
row only after the step body returns, so a body that raises leaves the ledger empty and
the handler re-raises. The worker then dead-letters the job with the step's
`failure_category`, and **that dead-lettered job's `error_category` (read via `jobs.get`)
is the authoritative failure signal** — identical to the build handler, whose
`test_build_handler_build_failure_sets_run_failed` asserts zero `run_steps` rows on a
failed build. Recovery is a **new Run** on the same System (the ADR-0026 §7 retry model),
not an in-place Run retry. A re-issued `runs.install`/`runs.boot` on a Run whose step
**succeeded** is the idempotent ledger replay; on a Run whose step failed (no row) it is
a fresh attempt of that step, not a resume of a failed one.

Boot's ordering gate is therefore "a **succeeded** `install` step exists": a failed
install records no row, so `runs.boot` after a failed install returns the same
`configuration_error` "install first" as after no install, and the agent reads the failed
`install` job for the reason.

This diverges from build, which *does* drive the Run `running → failed`: build owns the
Run-state transition because it is the step the Run state machine tracks; install/boot
are post-`succeeded` steps the state machine does not model, so they cannot transition a
terminal Run without an illegal edge.

### 3. The `crashkernel=` reservation is enforced at `runs.install`, synchronously, against the kernel command line

The acceptance pins "a kernel without `crashkernel=` is rejected at install
(`configuration_error`)". The reservation is a property of the **kernel command line**
the install plane sets (not of the build profile or the resolved `.config` — those are
the build plane's, ADR-0029 §3). So `runs.install` constructs the cmdline from the Run's
`build_profile` (an optional `cmdline`, defaulting to one that **includes**
`crashkernel=`) and rejects a cmdline missing a `crashkernel=` token **before** enqueuing
any job — an immediate, actionable `configuration_error`, matching `runs.build`'s
synchronous profile parse. This is distinct from the build plane's `CONFIG_CRASH_DUMP`
check: `CONFIG_CRASH_DUMP=y` compiles kdump *into* the kernel; `crashkernel=` *reserves*
the memory at boot. Both are needed; they are checked where each lives.

### 4. The kdump capture prerequisite is verified by the install handler, as `configuration_error`

The `crashkernel=` reservation is inert without a kdump capture service/initramfs that
actually saves a core (m0-walking-skeleton "kdump prerequisite"). The install handler
verifies the capture path is present (the same readiness machinery boot uses, run
against the staged config); an absent capture path is a `configuration_error` (a config
defect the operator fixes), distinct from a libvirt redefine failure (`install_failure`).

### 5. Realized `Installer` and `Booter` ports, distinct from the capability-dispatch `InstallPlane` placeholder

Mirroring the realized `Builder`/`Provisioner`/`Controller` ports, #19 introduces
realized ports the handlers depend on, keyed on the libvirt domain name the System
already minted, so unit tests inject fakes and the real libvirt/object-store path is
`live_vm`-gated:

```python
class Installer(Protocol):
    def install(self, system_id: UUID, kernel_ref: str, *, cmdline: str) -> None: ...
class Booter(Protocol):
    def boot(self, system_id: UUID) -> None: ...
```

`LocalLibvirtInstall` satisfies both — domain redefine to a direct-kernel `<os>`
(`<kernel>`/`<initrd>`/`<cmdline>`), power-cycle, and the readiness preflight — over the
same injected `connect` seam `control.py`/`provisioning.py` use, plus injected
object-store-fetch and readiness seams. Reconciling the `interfaces.InstallPlane`
placeholder with the realized port is deferred (install is not dispatched through the
capability registry in M0, matching build/provisioning/control).

### 6. Boot distinguishes `boot_timeout` (never came up) from `readiness_failure` (up but a check failed)

`boot()` power-cycles the domain into the staged `<kernel>` (destroy-then-create), then
polls the run-readiness preflight (the **port of v1 `prereqs/`**) until it passes or an
**injected boot-window timeout** elapses. Critically, libvirt `create()` returning means
QEMU launched, **not** that the guest kernel is up — so "the System came up" is observable
only through the readiness seam, and the two boot failures are pinned by *which* signal
the seam gives: the System **never answering** within the boot window is `boot_timeout`
("it never came up"); the System **answering** but a check failing (wrong kernel running,
kdump path not armed) is `readiness_failure` ("up but wrong"). A hard libvirt error on the
`destroy`/`create` itself (the domain cannot start) is `install_failure`. The boot window
is a constructor parameter, so the handler is unit-testable without a host or a real wait
and the salvaged v1 checks land behind one seam.

### 7. Idempotency reuses the build mechanisms; install/boot use the `run_step` helper directly

`runs.install`/`runs.boot` enqueue with `dedup_key = f"{run_id}:install"` /
`f"{run_id}:boot"` (one job per `(run_id, step)`), so a client retry returns the same
job. The handlers wrap their body in `run_step(conn, run_id, "install"|"boot", fn)`, so a
worker re-dispatch returns the stored result without re-staging/re-booting. Build rolled
its own ledger logic because `make` must run with **no DB transaction held** (30+ min);
install/boot carry no such long op, so they run inside the `run_step` transaction and use
the existing helper unchanged.

## Consequences

- The Run state machine is untouched: install/boot add no `RunState` and no schema
  migration; they record `run_steps` rows and surface failures via the step's
  `failure_category` and a dead-lettered job, leaving a built Run `succeeded`.
- A missing `crashkernel=` reservation is a synchronous `configuration_error`, the
  acceptance's pinned contract; the kdump capture prerequisite is verified where the
  config is staged.
- The tool/handler logic is fully unit-testable with fake `Installer`/`Booter` + fake
  store/readiness seams; the real libvirt redefine/power-cycle/readiness path is
  `live_vm`-gated, so CI stays green without a host.
- `mcp/tools/runs.py` gains two tools + two handlers + two lines in
  `register_handlers`; `mcp/app.py` is **unchanged** (`runs.register_handlers` already
  registers); `docs/adr/README.md` gains one index row. No other shared file is edited.

## Considered & rejected

- **Give install and boot their own `RunState`s (e.g. `installed`, `booted`).**
  Rejected: it would re-open the Run state machine #17/#18 pinned and the schema
  `runs_state_check` enum, and it contradicts ADR-0029, which already drives the Run
  `succeeded` on build. The walking skeleton's "idempotent steps keyed `(run_id, step)`"
  is the model; the `run_steps` ledger already carries per-step state and result.
- **Drive the Run `succeeded → failed` on an install/boot failure.** Rejected:
  `RunState` makes `succeeded` terminal (no such edge), and the build *did* succeed —
  marking the whole Run failed would lose that the kernel exists and is reusable. The
  dead-lettered job's `error_category` is the authoritative failure signal (no
  `run_steps` row is written on failure, §2); recovery is a new Run (ADR-0026 §7).
- **Check `crashkernel=` in the build plane / against the `.config`.** Rejected:
  `crashkernel=` is a boot-time *cmdline* reservation, not a compile-time config option
  (`CONFIG_CRASH_DUMP`, already checked by the builder). The reservation lives on the
  command line the install plane sets, so it is verifiable only there. The acceptance
  pins the rejection at **install**.
- **Re-supply the kernel/cmdline as `runs.install` arguments.** Rejected: the kernel is
  the Run's recorded `kernel_ref` and the cmdline derives from the Run's
  `build_profile`; re-supplying them would let an install disagree with the Run it
  targets, exactly as ADR-0029 rejected re-supplying the build profile. `runs.install`
  takes only `run_id`.
- **One `runs.install_and_boot` tool / one job.** Rejected: the spec's tool surface and
  the `(run_id, step)` dedup model are two distinct steps (`install`, `boot`) an agent
  enqueues and polls separately; collapsing them would lose the per-step idempotency and
  the `install → boot` ordering check, and a boot retry would re-stage the kernel.
- **Boot via `control.power(on)` reuse instead of a dedicated `Booter`.** Rejected: the
  control plane keys on a power *action* and owns no readiness preflight; boot needs the
  power-cycle **plus** the run-readiness preflight as one step, and a distinct realized
  port keeps the readiness logic and the `boot_timeout`/`readiness_failure` distinction
  with the plane that owns it. The power-cycle libvirt calls are the same primitives,
  not the same contract.
- **Collapse `boot_timeout` and `readiness_failure` into one category.** Rejected: the
  agent's recovery differs — `boot_timeout` (never came up) suggests an
  infrastructure/kernel-panic retry, `readiness_failure` (up but wrong) suggests a
  config fix; the taxonomy already separates them and the acceptance names both.
