# Install + boot plane (local libvirt) — design (issue #19)

- **Status:** Draft
- **Date:** 2026-06-04
- **Issue:** #19 (M0: Install + boot plane)
- **Depends on:** #18 (build plane — the `runs.*` surface, the `Builder`/realized-port
  pattern, the `run_steps` step ledger, and the `kernel_ref` this plane stages), #16
  (provisioning plane — the libvirt connection-factory seam and the domain tagged with
  its `system_id`, which this plane redefines with a direct-kernel `<os>`).
- **ADR:** [ADR-0030](../../adr/0030-install-boot-plane.md) (the open decisions this
  spec settles).

## 1. Problem

A Run reaches `succeeded` once its kernel is built (`kernel_ref` recorded, ADR-0029),
but the System is still running whatever it provisioned with — it has not booted the
built kernel. The install + boot plane adds the next two Run steps:

- `runs.install(run_id)` — stages the built `kernel_ref` (and an initrd) for the
  domain's **next boot** via libvirt direct-kernel boot, sets the kernel command line
  with a `crashkernel=` reservation, and verifies the kdump capture
  service/initramfs prerequisite. A kernel command line without a `crashkernel=`
  reservation is rejected at install with `configuration_error` (the acceptance).
- `runs.boot(run_id)` — boots the installed kernel (power-cycle the domain into the
  staged `<kernel>`), runs the **run-readiness preflight** (the port of v1 `prereqs/`)
  before declaring boot ready, and fails with `boot_timeout` (the System did not come
  up) or `readiness_failure` (it came up but a readiness check failed).

Both are **idempotent steps** keyed `(run_id, step)`, exactly like build: a re-issued
`runs.install`/`runs.boot` returns the same job and the worker never re-stages or
re-boots a step that already recorded a result.

## 2. Scope

In scope (the issue's files + tests):

- `src/kdive/providers/local_libvirt/install.py` — the realized `Installer` and
  `Booter` ports and their `LocalLibvirtInstall` implementation (redefine the domain
  for direct-kernel boot; power-cycle; readiness preflight), mirroring
  `providers/local_libvirt/build.py` and `…/control.py`.
- `src/kdive/mcp/tools/runs.py` — add the `runs.install`/`runs.boot` tools, the
  `install_handler`/`boot_handler` job handlers, and bind them in `register_handlers`,
  alongside (not disturbing) the existing build tool/handler.
- Wire the two handlers into `mcp/app.py`'s `_HANDLER_REGISTRARS` (the existing
  `runs.register_handlers` already registers there; it gains the two handlers, so
  `app.py` itself needs **no** new tuple entry — call out below).

Out of scope: the actual crash→vmcore capture (#21+), the gdbstub/debug transport
(#23+), vmcore symbolization (#22/#24). This plane stages, boots, and confirms
readiness; it does not crash the guest or fetch a core.

## 3. Where install + boot sit in the Run lifecycle

`RunState` is `created → running → succeeded|failed|canceled`, and the build plane
already drives `running → succeeded` once the kernel is built (ADR-0029 §5). The Run
state machine has **no further forward states** — install and boot do not get their own
Run states. The walking skeleton models `build → install → boot` as three **idempotent
steps of one Run** keyed `(run_id, step)`
([m0-walking-skeleton.md](../../specs/m0-walking-skeleton.md) "loop each of build,
install, boot — enqueue then poll jobs.wait … run_steps(step) result"), with the Run's
state tracking the **build** step (the one that produces the durable `kernel_ref`
columns). So:

- Install and boot are recorded **only** in the `run_steps` ledger (the generic
  `step` column already accepts any name; no schema change). They do **not** re-flip
  the Run's `state` column.
- A Run must be **`succeeded`** (built) before `runs.install`, and must have a
  **succeeded** `install` step before `runs.boot`. A Run not yet built
  (`created`/`running`) → `configuration_error` "build first"; a terminal
  `failed`/`canceled` Run → `configuration_error` (`data.current_status`) — you cannot
  install onto a dead Run.
- An install or boot **failure records no `run_steps` row** — exactly the build
  handler's contract: `run_step` (`src/kdive/db/idempotency.py`) records a
  `state='succeeded'` row **only after** the step body returns, and writes nothing on an
  exception, so a failed step leaves the ledger empty and the handler re-raises. The
  worker then dead-letters the job with the step's `failure_category`, and **that
  dead-lettered job's `error_category` is the authoritative failure signal** (read via
  `jobs.get`), not a ledger row. The failure does **not** drive the Run's `state` to
  `failed`: the Run's build genuinely succeeded and `RunState.SUCCEEDED` is terminal (no
  `succeeded → failed` edge), so the Run stays `succeeded` with the downstream job
  `failed`. (Recovery is a **new Run** on the same System, the ADR-0026 §7 retry model —
  re-running install on the same Run is the idempotent replay of the *succeeded* step, if
  one is recorded, never an in-place retry of a failed one.)
- Because a failed install records no row, the boot ordering gate ("a **succeeded**
  `install` step exists") cannot be satisfied by a failed install: `runs.boot` after a
  failed install returns `configuration_error` "install first", and the agent reads the
  failed `install` **job** (`jobs.get`) to see *why* install failed — the job, not the
  absent ledger row, carries that. This is the same shape as build: a failed build
  records no `build` ledger row (`test_build_handler_build_failure_sets_run_failed`
  asserts zero rows) and the failure lives on the job/Run.

This keeps the Run state machine exactly as #17/#18 pinned it and uses the ledger —
already the step idempotency mechanism — only for **succeeded**-step replay, with the
job carrying every failure (identical to the build handler's `run_step` discipline).

## 4. The realized ports: `Installer` and `Booter`

Mirroring the build plane's realized `Builder` (distinct from the capability-dispatch
`BuildPlane` placeholder), this plane introduces realized ports the handlers depend on,
so unit tests inject fakes and the real libvirt path is `live_vm`-gated:

```python
class Installer(Protocol):
    # run_id keys the per-Run staging path (§5.2); cmdline is the gated command line (§5.1).
    def install(self, system_id: UUID, run_id: UUID, kernel_ref: str, *, cmdline: str) -> None: ...

class Booter(Protocol):
    def boot(self, system_id: UUID) -> None: ...          # boot + readiness preflight
```

`LocalLibvirtInstall` satisfies both (one class owns the domain redefine + boot +
readiness, all keyed on the libvirt domain name `kdive-{system_id}` the provisioning
plane minted). It is constructed over the same injected `connect: Callable[[],
_LibvirtConn]` seam `control.py`/`provisioning.py` use, plus injected
`fetch_kernel`/`fetch_initrd` (object-store reads of `kernel_ref`) and a `readiness`
seam (the ported preflight) — so the unit tests never touch libvirt or S3.

These are distinct from `interfaces.InstallPlane` (`install(system, kernel)`), the
capability-dispatch placeholder; reconciling the placeholder with the realized port is
deferred, matching build/provisioning (install is not dispatched through the registry
in M0).

## 5. Install: direct-kernel boot staging + the `crashkernel=` gate

The provisioning plane deliberately rendered the domain **without** `<kernel>`/
`<cmdline>` (provisioning.py docstring: "the test kernel plus its `crashkernel=` kdump
reservation are the install/boot plane's"). `install()` redefines the existing tagged
domain to add an `<os>` direct-kernel section pointing at the staged kernel/initrd and
the command line:

1. **`crashkernel=` gate (synchronous, at the tool).** `runs.install` builds the
   kernel command line and rejects one without a `crashkernel=` token **before** any
   job is enqueued — a `configuration_error`, the acceptance's pinned category. The
   cmdline is derived from the Run's `build_profile` (an optional `cmdline` field) with
   a default that **includes** `crashkernel=`; an operator who overrides it and drops
   `crashkernel=` is rejected. The gate is at the tool boundary (like
   `runs.build`'s profile parse), so a missing reservation is an immediate, actionable
   rejection, not a dead-lettered job.
2. **Stage (handler).** The handler fetches `kernel_ref` (and the initrd) from the
   object store to a **per-Run** host-local staging path libvirt can read — keyed
   `…/{system_id}/{run_id}/{kernel,initrd}`, so a later Run on the same System stages to
   a distinct path and the redefine references *this* Run's kernel (never a stale
   overwrite of another Run's file). The fetch writes to a temp file and renames into
   place, so a fetch that fails partway leaves no partial file the redefine could point
   at. It then `defineXML`s the domain with the direct-kernel `<os>` (`<kernel>`,
   `<initrd>`, `<cmdline>`) referencing that per-Run path. The redefine is idempotent
   (libvirt `defineXML` overwrites the persistent config); the `run_steps('install')`
   ledger makes the *handler body* replay-safe so a succeeded re-dispatch re-fetches
   nothing. Staging-file cleanup is the System's teardown / the reconciler's leaked-domain
   sweep (a per-Run path under the System is removed when the System is torn down); M0
   does not GC mid-Run staging files, and this is stated, not left implicit.
3. **kdump prerequisite.** The handler verifies the kdump capture service/initramfs is
   present (the `crashkernel=` reservation alone is inert without the capture path).
   In M0 this is the same readiness-preflight machinery boot uses, run against the
   staged config; a missing capture path is a `configuration_error` (a config defect
   the operator fixes), distinct from a libvirt redefine failure (`install_failure`).

A libvirt error staging/redefining the domain is `install_failure`.

## 6. Boot: power-cycle + run-readiness preflight

`boot()` boots the installed kernel and confirms the System is up **on it**:

1. **Boot.** Power-cycle the domain into the staged `<kernel>` (destroy if running,
   then create — the domain now boots the direct-kernel `<os>`). **`create()` returning
   means QEMU launched, not that the guest kernel is up** — libvirt does not block on or
   report guest readiness — so "the System came up" is *only* observable through the
   readiness preflight (step 2), never from `create()` itself. A hard libvirt error on
   `destroy`/`create` (the domain cannot be started at all) is `install_failure`.
2. **Readiness preflight (the port of v1 `prereqs/`), with an explicit boot window.**
   Before declaring boot ready, poll the run-readiness checks (the System answers, the
   expected kernel is running, the kdump path is armed) until they pass or an **injected
   boot-window timeout** elapses. The preflight is an injected `readiness` seam returning
   a structured result, and the two boot failures are pinned by *which* signal the seam
   gives:
   - **`boot_timeout`** — the System **never answered** within the boot window (no
     response / not reachable before the timeout). This is the "it never came up" case;
     `create()` succeeding does not contradict it (QEMU launched but the guest never
     reached userspace).
   - **`readiness_failure`** — the System **answered**, but a check failed (the wrong
     kernel is running, or the kdump capture path is not armed). It came up, but not
     correctly.

   The v1 `prereqs/` module is **ported** behind this seam; the salvaged checks land here
   ([m0-walking-skeleton.md](../../specs/m0-walking-skeleton.md) "run-readiness
   preflight → Run lifecycle (pre-`boot` readiness)"). The boot window is a constructor
   parameter (default a small bound), so tests drive both the timeout and the
   answered-but-failed paths with a fake seam and no real wait.
3. **Record.** On success the `boot` step records its result (the booted kernel
   identity / readiness summary) in the ledger; the Run stays `succeeded`.

`runs.boot` requires a **succeeded** `install` step (you cannot boot what was not
installed). With no succeeded `install` ledger row → `configuration_error` "install
first"; a *failed* prior install leaves no row (§3), so it reads the same way, and the
agent inspects the failed `install` **job** (`jobs.get`) for the reason.

## 7. Idempotency: the same two mechanisms as build

- **Job dedup** — `runs.install` enqueues `JobKind.INSTALL` with
  `dedup_key = f"{run_id}:install"`; `runs.boot` enqueues `JobKind.BOOT` with
  `dedup_key = f"{run_id}:boot"`. A client retry returns the same job (the
  `(run_id, step)` dedup_key ADR-0026 §7 reserves for each plane).
- **Step ledger** — `install_handler`/`boot_handler` wrap their body in
  `run_step(conn, run_id, "install"|"boot", fn)`. On **success** `run_step` records the
  `state='succeeded'` row and a re-dispatch returns the stored result without
  re-staging/re-booting. On **failure** the body raises *before* `run_step` writes
  anything, so no row is recorded and the handler re-raises for the worker to
  dead-letter (§3) — this is `run_step`'s built-in contract, identical to the build
  handler's, not a separate failure-recording path. Unlike build, the install/boot
  bodies hold **no** 30-minute `make`, so they run inside the `run_step` transaction
  directly (build rolled its own ledger only because `make` must run with no DB
  connection held; install/boot have no such constraint and use the existing `run_step`
  helper unchanged).

## 8. Failure-category table

| Failure | Category | Where |
|---------|----------|-------|
| cmdline missing `crashkernel=` | `configuration_error` | `runs.install` (synchronous) |
| Run not built (`created`/`running`) | `configuration_error` (`current_status`) | `runs.install` |
| Run terminal (`failed`/`canceled`) | `configuration_error` (`current_status`) | `runs.install`/`runs.boot` |
| `runs.boot` with no **succeeded** `install` step | `configuration_error` | `runs.boot` |
| kdump capture path absent | `configuration_error` | install handler |
| libvirt redefine/stage error | `install_failure` | install handler |
| readiness seam: System never answered within the boot window | `boot_timeout` | boot handler |
| readiness seam: answered but a check failed (wrong kernel / kdump not armed) | `readiness_failure` | boot handler |
| libvirt `destroy`/`create` hard error (domain cannot start) | `install_failure` | boot handler |

All are existing `ErrorCategory` members (`schema 0001` already lists `install_failure`,
`boot_timeout`, `readiness_failure` in the `runs.failure_category` and
`jobs.error_category` checks, and `boot` in the audit `tool` check). **No migration.**
Every handler failure surfaces as the dead-lettered **job's** `error_category` (read via
`jobs.get`); no failure writes a `run_steps` row (§3).

## 9. Test plan (TDD, handlers called directly)

- **Installer/Booter unit** (`tests/providers/local_libvirt/test_install.py`, fake
  libvirt conn + fake fetch/readiness seams): redefine renders a direct-kernel `<os>`
  with `<kernel>`/`<initrd>`/`<cmdline>` referencing the per-Run staging path; the
  cmdline carries `crashkernel=`; a libvirt redefine error → `install_failure`; the
  fetch renames into place (a failed fetch leaves no partial file); boot power-cycles
  and polls readiness; the readiness seam's *never-answered* → `boot_timeout`; its
  *answered-but-failed* → `readiness_failure`; a `destroy`/`create` libvirt error →
  `install_failure`; the boot window is an injected bound so neither path waits;
  `from_env` does not connect.
- **`runs.install`/`runs.boot` tool** (`tests/mcp/test_runs_tools.py`, real migrated
  DB, injected ctx): a cmdline without `crashkernel=` → `configuration_error` (no job);
  install on a `succeeded` Run flips no Run state and enqueues one `install` job;
  idempotent re-issue returns the same job; install on a `created`/`running` Run →
  `configuration_error`; boot with no succeeded `install` ledger row →
  `configuration_error` (covering both "never installed" and "install failed, no row");
  boot **after** a seeded succeeded `install` step → enqueues one `boot` job; terminal
  Run → `configuration_error`; non-operator raises; cross-project/missing/bad-uuid →
  `configuration_error`.
- **`install_handler`/`boot_handler`** (real DB, fake Installer/Booter): a succeeding
  handler records exactly one `install`/`boot` ledger row (Run stays `succeeded`);
  replay does not re-stage/re-boot (the builder/booter fake records one call); an
  install/boot **failure writes no `run_steps` row** (assert zero rows, mirroring
  `test_build_handler_build_failure_sets_run_failed`) and re-raises the category, and
  the Run is unchanged (still `succeeded`); `register_handlers` binds both
  `JobKind.INSTALL` and `JobKind.BOOT`.
- **live_vm gated**: the real libvirt redefine + power-cycle + readiness against a host
  (`pragma: no cover`, `@pytest.mark.live_vm`), mirroring `test_build.py`'s gated case.

## 10. Shared-file edits (called out for the concurrent sibling #24)

- `src/kdive/mcp/tools/runs.py` — **additive**: new tools + handlers; the existing
  `build_*` functions and `register`/`register_handlers` are extended, not changed in
  behavior.
- `docs/adr/README.md` — append the ADR-0030 index row (one line).
- `src/kdive/mcp/app.py` — **no edit expected**: `runs.register_handlers` already sits
  in `_HANDLER_REGISTRARS`; it gains the two handlers internally. If a registrar
  signature must change, it is called out here.
- `src/kdive/domain/errors.py`, `db/schema/0001_init.sql` — **no edit**: every category
  and `step`/`kind`/`tool` value already exists.
