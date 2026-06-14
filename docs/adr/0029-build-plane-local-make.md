# ADR 0029 — Build plane (local make): runs.build, BuildProfile, build handler (M0)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** #18 (M0: Build plane — local make)
- **Depends on:** [ADR-0011](0011-provisioning-profile-schema.md) /
  [ADR-0024](0024-provisioning-profile-model-shape.md) (the profile-schema pattern
  `BuildProfile` mirrors), [ADR-0018](0018-job-queue-worker-execution.md) (the job
  queue / dedup_key / worker the build runs on),
  [ADR-0025](0025-provisioning-plane-libvirt.md) (the realized-port + handler pattern
  this mirrors), [ADR-0026](0026-investigation-run-lifecycle.md) (the Run lifecycle,
  the `(run_id, step)` dedup_key it hands to this plane, and the `build_profile` jsonb
  it leaves opaque), [ADR-0016](0016-repository-layer-locks-idempotency.md) (the
  advisory locks and the `run_steps` step ledger),
  [ADR-0013](0013-object-store-layout-retention.md) /
  [ADR-0017](0017-object-store-client-interface.md) (the artifact store).
- **Refines:** the M0 Build wording in
  [`../specs/m0-walking-skeleton.md`](../design/m0-walking-skeleton.md) ("Local-libvirt
  provider → Build", "Exit criteria → Idempotency") and the issue-18 scope in
  [`../plans/m0-implementation.md`](../archive/plans/m0-implementation.md).
- **Spec:** [`../superpowers/specs/2026-06-04-build-plane-design.md`](../archive/superpowers/specs/2026-06-04-build-plane-design.md)

## Context

A Run is created on a `ready` System but holds no kernel. Issue #18 adds the build
plane: `runs.build` builds a kernel from source as an **idempotent job** and records
two artifacts on the Run — the bootable `kernel_ref` (install plane, #19/#20) and a
build-id-keyed `vmlinux`/`debuginfo_ref` (postmortem, #22/#24). The `Run` model,
`runs` columns (`kernel_ref`, `debuginfo_ref`, `failure_category`), the `JobKind.BUILD`
enum, the `run_steps` ledger, and the `BuildPlane`/`BuildProfile` placeholders all
already exist; #18 adds the schema, the realized builder, the tool, and the handler.

Several decisions are left open by the parent spec and ADR-0026; they are settled here.

## Decision

### 1. Two idempotency mechanisms, for two failure modes: job dedup *and* the step ledger

`runs.build` enqueues `JobKind.BUILD` with `dedup_key = f"{run_id}:build"`, the
`(run_id, step)` dedup_key [ADR-0026 §7](0026-investigation-run-lifecycle.md)
explicitly hands to the build plane. A client retry of `runs.build` therefore returns
the **same** job row (in whatever state it has reached) — no duplicate job, no second
build. That satisfies the acceptance "a re-issued `runs.build` returns the same job".

Separately, the **handler body** is wrapped in `run_step(conn, run_id, "build", fn)`,
so a *worker* re-dispatch of the same job (lease lapse → double-run, or a requeue
after a transient finalize error) returns the stored `{kernel_ref, debuginfo_ref,
build_id}` without re-running `make`. That satisfies exit-criterion 4 ("replaying a
completed step returns the prior result without re-executing").

The dedup_key bounds *admission* (one job per Run); the step ledger bounds *execution*
(one `make` per job, across worker re-dispatch). They are not redundant — neither
alone covers both the client-retry and the worker-redispatch path.

### 2. The `BuildProfile` is the opaque `build_profile` already on the Run; `runs.build` takes only `run_id`

ADR-0026 §6 stored `build_profile` as opaque jsonb at `runs.create` and deferred its
validation here. `runs.build` therefore takes **only `run_id`** — it does not re-supply
the profile. The handler reads `run.build_profile` and parses it through
`BuildProfile.parse()`. A profile that fails to parse is a `configuration_error`
surfaced **synchronously** by `runs.build` (before any job is enqueued), so a malformed
profile is an immediate, actionable rejection, not a dead-lettered job — matching how
`systems.provision` parses its profile at the tool boundary.

`BuildProfile` is `frozen`, `extra="forbid"`, parsed through a single `parse()` that
maps `ValidationError → configuration_error` and **scrubs submitted values** from the
error details, identical to `ProvisioningProfile`. Fields: `schema_version: Literal[1]`,
`kernel_source_ref`, `config_ref`, `patch_ref: str | None = None`.

### 3. The kdump/debuginfo config requirements are enforced by the builder against the resolved `.config`, not by parsing the profile

`CONFIG_CRASH_DUMP`/`crashkernel` and `CONFIG_DEBUG_INFO(_DWARF)`/BTF are properties of
the resolved kernel `.config`, not of the profile document — the profile only *names*
the config (`config_ref`). So they are checked by the builder against the kernel tree
at build time (a config preflight before `make`), where the config actually lives, not
by `BuildProfile.parse()`. A config that omits a required option is a
`configuration_error` (a config/profile defect the operator fixes), distinct from a
`make` failure (`build_failure`); both drive the Run to `failed`.

### 4. A realized `Builder` port (returning two refs + the build-id), distinct from the capability-dispatch `BuildPlane` placeholder

The capability-dispatch `BuildPlane` Protocol (`build(run, profile) -> KernelArtifact`)
returns a *single* artifact and keys on the Run; #18 needs **two** artifacts plus the
build-id. So, exactly as the provisioning plane introduced a realized `Provisioner`
port distinct from the `ProvisioningPlane` placeholder, #18 introduces a realized
`Builder` port:

```python
class BuildOutput(NamedTuple): kernel_ref: str; debuginfo_ref: str; build_id: str
class Builder(Protocol): def build(self, run_id: UUID, profile: BuildProfile) -> BuildOutput: ...
```

`LocalLibvirtBuild` satisfies it: warm-tree checkout (base + optional patch), config
preflight, incremental `make`, GNU build-id extraction from the produced `vmlinux`, and
two `sensitive` artifact puts. Reconciling the placeholder Protocol with the realized
port is deferred (build is not dispatched through the capability registry in M0,
matching provisioning). The handler depends on the `Builder` Protocol, so unit tests
inject a fake builder — the real `make`/object-store path is exercised only under the
existing **`live_vm`** kernel-build gate.

### 5. The Run is driven `created → running → succeeded|failed`; `runs.build` is admission-idempotent on Run state

`RunState` already pins `created → running` and `running → succeeded|failed`.
`runs.build` drives `created → running` and enqueues the job in **one transaction**
under a per-Run advisory lock, so the Run is never `running` with no job (or a job with
no flip), and two concurrent `runs.build` calls produce exactly one job. By Run state:

| Run state at `runs.build` | result |
|---------------------------|--------|
| `created` | flip `created → running`, enqueue the build job, return the handle |
| `running` | a build is already admitted — re-enqueue is a dedup no-op; return the **same** job |
| `succeeded` | already built — return the existing (succeeded) job; **no rebuild** |
| `failed` / `canceled` | terminal — `configuration_error` (`data.current_status`); a failed build is not retried in place (retry-as-a-new-Run, ADR-0026 §7) |

The handler finalizes under the same per-Run lock: it writes `kernel_ref` +
`debuginfo_ref` and `running → succeeded` in one fenced `UPDATE … WHERE id = … AND
state = 'running'`, then audits — the "write the columns and the transition together
under the lock" shape the provision handler uses for `domain_name` + `ready`. On a
build failure it drives `running → failed` with the build's category, tolerating
`IllegalTransition` if a concurrent cancel already won, then re-raises so the worker
dead-letters the job with the correct category.

### 6. `LockScope` gains the reserved `RUN` member; the global lock order is unchanged

ADR-0026 already reserved `RUN` last in the global order `ALLOCATION → SYSTEM →
INVESTIGATION → RUN` but added no enum member ("no M0 tool needs a per-Run lock yet").
#18 is that tool: `LockScope` gains `RUN = "run"`. Both `runs.build` and the handler
take only the `RUN` lock (no other scope at once), so the global order is not
exercised across scopes here and needs no change.

## Consequences

- A client retry of `runs.build` returns the same job and never rebuilds (dedup_key);
  a worker re-dispatch never re-runs `make` (step ledger). The two acceptance
  idempotency clauses are met by two distinct, already-existing mechanisms.
- A malformed `build_profile` is a synchronous `configuration_error`, not a
  dead-lettered job; the build-config prerequisites are enforced where the config
  lives (the builder), with the most specific category.
- The handler/tool logic is fully unit-testable with a fake `Builder` + fake store; the
  real `make`/object-store path is `live_vm`-gated, so CI stays green without a
  toolchain.
- The Run carries both refs and the build-id; #19/#20 (install) read `kernel_ref` and
  #22/#24 (symbolization) read `debuginfo_ref`, with no further build-plane change.
- `LockScope` gains one additive member; `mcp/app.py` gains the build handler in
  `_HANDLER_REGISTRARS` (one tuple append); `runs.py` gains the tool + handler +
  `register_handlers`.

## Considered & rejected

- **Have `runs.build` re-take the `build_profile` as a tool argument.** Rejected: the
  profile already lives on the Run (`runs.create` stored it), and re-supplying it would
  let a `runs.build` disagree with the Run's recorded profile — a Run builds the profile
  it was created with. Reading `run.build_profile` keeps a single source of truth and
  matches "`runs.build(run_id)`" in the tool surface.
- **Validate the kdump/debuginfo config options in `BuildProfile.parse()`.** Rejected:
  the profile names a config by reference; the *contents* are not in the profile
  document, so a parse-time check could not see them. The builder resolves the config
  and checks it against the tree, where the requirement is actually verifiable.
- **One artifact / reuse the `BuildPlane` placeholder's single-`KernelArtifact`
  return.** Rejected: the acceptance and the symbolization planes require a *separate*
  build-id-keyed debuginfo artifact; collapsing them would lose the vmcore-symbolization
  input. The realized `Builder` returns both refs and the build-id.
- **Rely on the job dedup_key alone for idempotency (drop the step ledger).** Rejected:
  the dedup_key stops a *second job*, but a *single* job re-dispatched after a lease
  lapse would re-run `make` (minutes of wasted work, and a second set of artifacts).
  The step ledger is what makes the handler body itself replay-safe (exit-criterion 4).
- **Rely on the step ledger alone (drop the dedup_key).** Rejected: without the
  dedup_key, two `runs.build` calls would enqueue two jobs; the ledger would serialize
  the `make`, but a second job is still admitted and dispatched — the acceptance is "the
  same job", not "two jobs that happen to share a result".
- **Retry a `failed` build in place (`failed → running`).** Rejected: `RunState` makes
  `failed` terminal, and ADR-0026 §7 fixes the retry model as a *new Run* on the same
  System. Re-running a failed Run would need a non-terminal `failed` and a re-build
  decision the milestone defers.
- **Add a `RUNS.update_refs` repository method.** Rejected: the ref-write is a single
  fenced `UPDATE` local to the handler (it must be atomic with the `running → succeeded`
  transition under the per-Run lock); a generic repository method would either split the
  write from the transition or duplicate the fence. The inline `UPDATE` is the same
  approach the provision handler uses for `domain_name` + `ready`.
- **Make the config-preflight failure `build_failure`.** Rejected: a config missing the
  kdump/debuginfo options is a profile/config defect the operator fixes, not a toolchain
  failure; `configuration_error` is the more specific, more actionable category. The
  acceptance pins `build_failure` for "a failing build" (a `make` failure), which this
  preserves.
