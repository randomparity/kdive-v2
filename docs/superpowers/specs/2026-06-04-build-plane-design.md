# Build plane (local make) — design (issue #18)

- **Status:** Draft
- **Date:** 2026-06-04
- **Issue:** #18 (M0: Build plane — local make)
- **Depends on:** #13 (provisioning-profile schema, the `ProvisioningProfile`
  pattern this mirrors), #17 (Investigation + Run lifecycle, the `runs.*` surface and
  the `Run`/`run_steps`/idempotency ledger this builds on).
- **ADR:** [ADR-0029](../../adr/0029-build-plane-local-make.md) (the open decisions
  this spec settles).

## 1. Problem

A Run is created on a `ready` System but carries no kernel yet. The build plane adds
`runs.build`: it builds a kernel from source as an **idempotent job** and records two
artifacts on the Run —

- `kernel_ref`: the bootable kernel image the install plane (#19/#20) stages, and
- `debuginfo_ref`: a build-id-keyed `vmlinux`/debuginfo artifact the postmortem
  planes (#22/#24) load to symbolize a vmcore (the port of v1 `symbols/`).

A re-issued `runs.build` must return the **same** job without rebuilding (dedup); a
failing build must drive the Run to `failed` with `build_failure`.

## 2. Scope

In scope (the issue's three files + tests):

- `src/kdive/profiles/build.py` — the `BuildProfile` Pydantic schema and its parse
  boundary (mirrors `profiles/provisioning.py`).
- `src/kdive/providers/local_libvirt/build.py` — the realized `Builder` port and its
  `LocalLibvirtBuild` implementation (`make` in a warm workspace), mirroring
  `providers/local_libvirt/provisioning.py`.
- `src/kdive/mcp/tools/runs.py` — add the `runs.build` tool, the `build_handler` job
  handler, and `register_handlers` (the worker seam), mirroring `systems.py`.
- Wire the build handler into `mcp/app.py`'s `_HANDLER_REGISTRARS`.

Out of scope: install/boot (#19/#20), the actual kdump capture (#21+), vmcore
symbolization (#22/#24). This plane only *produces and records* the two artifacts and
the build-id that those later planes consume.

## 3. The two mechanisms: job dedup and the step ledger

Two distinct idempotency mechanisms already exist and are both used, for two
different failure modes:

1. **Job dedup (`jobs.dedup_key`)** — admits the build job at most once per Run.
   `runs.build` enqueues `JobKind.BUILD` with `dedup_key = f"{run_id}:build"`
   ([ADR-0026 §7](../../adr/0026-investigation-run-lifecycle.md) hands this
   `(run_id, step)` dedup_key to the build plane). A client retry of `runs.build`
   returns the **same** job row in whatever state it has reached — no second job, no
   second build.

2. **Step ledger (`run_steps`)** — records that `(run_id, "build")` already produced a
   result, so a worker re-dispatch of the **same** build job (lease lapse → double-run,
   or a requeue after a transient finalize failure) does not re-run `make`.

   **The ledger is consulted and recorded around `make`, never across it.** `make` and
   the artifact store are slow (30+ min) and `make` runs **with no DB connection or
   transaction held** — the worker's explicit contract (the handler "holds no
   transaction across the handler … and commits its own steps",
   `jobs/worker.py`). So the handler does **not** call `kdive.db.idempotency.run_step`
   (which runs `fn()` *between* its `SELECT` and `INSERT` on one held connection, with
   no internal `conn.transaction()`, leaving the slow body inside an open implicit
   transaction). Instead it uses the ledger row directly in three short, separately
   bounded steps (§6b): (a) a short read for an existing `(run_id, "build")` result;
   (b) if absent, run `make` + store artifacts **connectionless**; (c) a short
   `conn.transaction()` that records the ledger row **and** finalizes the Run together.

The job dedup_key bounds *admission*; the ledger bounds *execution*. Acceptance
"a re-issued `runs.build` returns the same job and does not rebuild" needs the
former; exit-criterion 4 ("replaying a completed step returns the prior result
without re-executing") needs the latter.

**Crash window (store-then-record).** Steps (b) and (c) are not one atomic unit across
an object store + Postgres: a worker crash/lease-lapse after the artifacts are stored
but before the ledger row commits leaves the artifacts written and no ledger row, so a
re-dispatch re-runs `make`. To make that re-run *recover* rather than *orphan*, the two
artifact object keys are **deterministic** — `{tenant}/runs/{run_id}/{kernel,vmlinux}`
(the existing `{tenant}/{kind}/{object_id}/{name}` scheme) — so a re-run **overwrites**
the same keys (object-store `put` is last-writer-wins on a key) rather than leaking a
second pair. "Does not rebuild" is therefore guaranteed **once the ledger row is
committed**; before that commit a crash costs at most one wasted (idempotent,
self-overwriting) rebuild — a transient, recoverable state, not an artifact leak. This
window is the build-plane analogue of the reconciler-covered races in ADR-0026.

## 4. `BuildProfile` schema (`profiles/build.py`)

A versioned, declarative document, **frozen** and `extra="forbid"`, parsed through a
single `BuildProfile.parse()` that maps Pydantic's `ValidationError` onto
`configuration_error` and **scrubs submitted values** out of the error details (the
redaction guarantee, identical to `ProvisioningProfile.parse`). Fields:

| field | type | meaning |
|-------|------|---------|
| `schema_version` | `Literal[1]` | versioned; a non-`int` is rejected before `Literal` coercion (the `True`/`1.0` trap), exactly as `ProvisioningProfile` |
| `kernel_source_ref` | `NonEmptyStr` | the base source tree ref the warm tree is checked out at |
| `config_ref` | `NonEmptyStr` | reference to the kernel `.config` to build with |
| `patch_ref` | `NonEmptyStr \| None = None` | optional patch applied on top of the base tree (the incremental delta) |

The profile is the **opaque `build_profile` jsonb** already stored on the Run by
`runs.create` (ADR-0026 §6 deferred its validation here). `runs.build` does **not**
re-supply it; the handler reads `run.build_profile` and parses it. A `build_profile`
that fails to parse is a `configuration_error` surfaced **synchronously** by
`runs.build` (before any job is enqueued), so a malformed profile is an immediate,
actionable rejection rather than a dead-lettered job — matching how
`systems.provision` parses the profile at the tool boundary.

The build *config-correctness* requirements (`CONFIG_CRASH_DUMP`/`crashkernel`,
`CONFIG_DEBUG_INFO(_DWARF)`/BTF) are **not** validated by parsing the profile — they
are a property of the resolved `.config`, checked by the builder against the kernel
tree at build time (§5), where the config actually lives. The profile only *names*
the config; the builder enforces it.

## 5. The `Builder` port and `LocalLibvirtBuild` (`providers/local_libvirt/build.py`)

Mirroring the `Provisioner` realized port (distinct from the capability-dispatch
`BuildPlane` placeholder, which keys on the Run and returns a single artifact):

```python
class BuildOutput(NamedTuple):
    kernel_ref: str       # object-store key of the bootable kernel image
    debuginfo_ref: str    # object-store key of the build-id-keyed vmlinux/debuginfo
    build_id: str         # the kernel's GNU build-id (hex), the symbolization key

class Builder(Protocol):
    def build(self, run_id: UUID, profile: BuildProfile) -> BuildOutput: ...
```

`LocalLibvirtBuild` implements `build()`:

1. **Warm workspace** — check out the base tree at `kernel_source_ref` in the
   per-build workspace (the warm tree is the build-workspace setup, not a cold
   clone), apply `patch_ref` if present, and stage `config_ref` as `.config`.
2. **Config preflight** — assert the resolved `.config` enables
   `CONFIG_CRASH_DUMP`/`crashkernel` (kdump prereq) and
   `CONFIG_DEBUG_INFO`/`CONFIG_DEBUG_INFO_DWARF`/BTF (symbolization prereq). A missing
   required option is a `CONFIG_ERROR`-shaped failure raised **before** `make`
   (a config defect, not a build defect — see §7 for the category split).
3. **`make`** — run `make` in the workspace (incremental from the warm tree). A
   non-zero `make` exit is a `BUILD_FAILURE`.
4. **Extract the build-id** — read the GNU build-id from the produced `vmlinux`
   (the same id the booted kernel reports via `/sys/kernel/notes`), so #22/#24 can
   match debuginfo to the booted kernel.
5. **Store two artifacts** — put the bootable image and the `vmlinux`/debuginfo into
   the object store as `sensitive` artifacts (build outputs may embed local paths and
   config), under **deterministic, Run-keyed** object keys (`name` segments `kernel`
   and `vmlinux`, giving `{tenant}/runs/{run_id}/{kernel,vmlinux}`), so a re-run after
   a crash overwrites the same keys rather than orphaning a second pair (§3 crash
   window). Return the two object keys and the build-id.

The real `make`-driven, object-store-touching path runs only against a toolchain +
warm tree, so it is exercised under the **`live_vm`** gate (the existing kernel-build
gate, per the m0 plan). Unit tests inject a **fake `Builder`** (and the handler is
tested with that fake), so the handler/tool logic is fully covered without a
toolchain — exactly as the provisioning handler is tested with a fake `Provisioner`.

## 6. `runs.build` tool and `build_handler` (`mcp/tools/runs.py`)

### 6a. `runs.build` (synchronous admission)

`build_run(pool, ctx, run_id)`:

1. RBAC: `operator` (a build mutates the Run; matches `runs.create`). A non-member
   project gets the not-found-shaped `configuration_error` (no existence leak).
2. Load the Run; reject a missing/cross-project Run with `configuration_error`.
3. **Parse `run.build_profile`** → `configuration_error` synchronously on failure.
4. Gate on Run state (under a per-Run advisory lock — `LockScope` gains a `RUN`
   member, last in the global `ALLOCATION → SYSTEM → INVESTIGATION → RUN` order):
   - `created`: drive `created → running` and `enqueue` the build job, in one
     transaction under the lock.
   - `running` / `succeeded`: a build is already admitted (or done) — call `enqueue`
     with the same `dedup_key`, which is a **DO-NOTHING no-op that returns the existing
     job row** (`queue.enqueue` is upsert-then-fetch-by-dedup_key, so it returns the
     same job in whatever state it has reached, including `succeeded`). Idempotent; does
     not rebuild. No new Run transition.
   - `failed` / `canceled`: terminal — `configuration_error` (`data.current_status`).
     A failed build is not retried in place (the retry-as-a-new-Run model, ADR-0026).
5. Return the job-handle envelope (carrying `run_id` in `data`, like
   `_system_job_envelope`). Every non-terminal branch returns the **same** job via the
   one `enqueue` call, so the tool has a single uniform exit and the dedup guarantee is
   structural, not branch-by-branch.

The `created → running` flip and the `enqueue` share **one transaction** under the
`RUN` lock, so the Run is never left `running` with no job (or vice versa), and two
concurrent `runs.build` calls produce exactly one job (the second observes `running`
and re-enqueues into the same dedup_key row).

### 6b. `build_handler` (the worker)

`build_handler(conn, job, builder, store)`. The handler **never holds a DB transaction
across `make`** (the worker contract). It runs in four bounded steps:

1. **Resolve.** `run_id` comes from `job.payload` (just the id — the Run is the source
   of truth for everything else; no `system_id` is copied into the payload, so there is
   no second copy to drift from `run.system_id`). Read the Run; parse
   `run.build_profile`. Reconstruct the audit context from `job.authorizing` (the
   `_ctx_from_job` pattern).
2. **Ledger read (short).** In a short `conn.transaction()`, look up the
   `(run_id, "build")` `run_steps` row. If it exists, the build already produced its
   result `{kernel_ref, debuginfo_ref, build_id}` — **skip to step 4** with that stored
   result (no rebuild). The connection is released after this read; nothing is held
   across step 3.
3. **Build (connectionless).** With **no DB connection held**, call
   `builder.build(run_id, profile)` (the slow `make` + the two artifact puts, the
   builder offloads the sync store via `asyncio.to_thread`). The builder stores under
   deterministic Run-keyed object keys (§5), so a crash-and-retry overwrites rather than
   orphans. On a `CategorizedError` here, go to the failure path below.
4. **Record + finalize (short, under the `RUN` lock).** In **one** short
   `conn.transaction()` holding `advisory_xact_lock(RUN, run_id)`: (a) record the
   `(run_id, "build")` ledger row (`ON CONFLICT … DO NOTHING` — a concurrent double-run
   already recorded it); (b) re-read the Run state `FOR UPDATE`; (c) **only if** it is
   still `running`, `UPDATE runs SET kernel_ref=…, debuginfo_ref=…, state='succeeded'`
   and audit `running → succeeded`. If the Run is already `succeeded` (a same-job
   double-run finalized first) it is a no-op success; if a concurrent cancel drove it
   `canceled`, leave it (the built artifacts are inert, reachable for a re-run's
   overwrite). The lock is held only for this short write — **never across `make`**,
   mirroring how the provision handler takes the SYSTEM lock only for the post-`provision`
   state write, not across the libvirt call.

**Failure path.** On a `CategorizedError` from step 3, open a short `conn.transaction()`
under the `RUN` lock, drive `running → failed` with the error's category (the builder
raises `BUILD_FAILURE` for a `make` failure, `CONFIGURATION_ERROR` for a config-preflight
failure — set as the Run's `failure_category`), audit, and re-raise so the worker
dead-letters the job with the correct category. Tolerate `IllegalTransition` on the
failed-write (a concurrent cancel already drove the Run terminal), matching the provision
handler. A build failure is **not** recorded in the ledger (the step did not succeed), so
a retry of the job re-attempts `make` until `max_attempts` — at which point the job
dead-letters and the Run is `failed`.

`register_handlers(registry, *, builder=None, store=None)` builds the builder/store
lazily from env (no toolchain/S3 connection at registration), mirroring
`systems.register_handlers`, and registers `JobKind.BUILD`.

### 6c. Persisting the refs

`RUNS.update_state` only writes `state`, so step 4 sets `kernel_ref`/`debuginfo_ref`
with a direct `UPDATE runs SET kernel_ref=%s, debuginfo_ref=%s, state='succeeded'
WHERE id=%s AND state='running'` — one write, fenced on `running`, inside the §6b step-4
transaction. No new repository method is added (the fenced inline `UPDATE` is local to
the handler), mirroring the provision handler's `domain_name` + `ready` write.

## 7. Error categories

| condition | category | where |
|-----------|----------|-------|
| `build_profile` unparseable / missing required field | `configuration_error` | `runs.build` (synchronous) |
| Run missing / cross-project / terminal | `configuration_error` | `runs.build` (synchronous) |
| caller lacks `operator` | raises `AuthorizationError` | `runs.build` (no authz category, ADR-0020) |
| `.config` missing a required kdump/debuginfo option | `configuration_error` | builder preflight → handler → Run `failed` |
| `make` non-zero exit | `build_failure` | builder → handler → Run `failed` |
| object-store put fails | `infrastructure_failure` | store → handler → Run `failed` |
| build target Run gone mid-job | `infrastructure_failure` | handler |

The config-preflight failure is `configuration_error`, not `build_failure`: a kernel
config that omits the kdump/debuginfo prerequisites is a profile/config defect the
operator must fix, distinct from a toolchain/`make` failure. Both still drive the Run
to `failed` (the acceptance pins `build_failure` for "a failing build"; a config-
preflight rejection is a *pre-build* rejection, reported with the more specific
`configuration_error`).

## 8. Redaction

Build outputs (the `vmlinux`, the kernel image) may embed local build paths and the
resolved config; they are stored `sensitive` and are reachable only via
`artifacts.get` (#19), never inlined in a response. The `runs.build` and job-handle
envelopes carry only object **keys** (`kernel_ref`/`debuginfo_ref`) and the build-id —
references, never content — so the "references, never log dumps" rule holds
structurally. `BuildProfile.parse` scrubs submitted values from error details so a
profile referencing secret material cannot leak it.

## 9. Acceptance mapping

| acceptance clause | how it is met | test |
|-------------------|---------------|------|
| build job produces a kernel image **and** a debuginfo artifact, sets `kernel_ref`/`debuginfo_ref` | handler stores both, writes both columns under the lock | handler test (fake builder/store): both columns set, Run `succeeded` |
| debuginfo build-id is the `vmlinux`'s GNU build-id (the symbolization key) | builder extracts the GNU build-id from the produced `vmlinux`; it keys `debuginfo_ref` and flows to the result | `live_vm` build test asserts the extracted id equals `readelf -n vmlinux` (extraction tested against a real ELF); unit test pins the fake's build-id through the handler to `debuginfo_ref` |
| re-issued `runs.build` returns the same job (dedup), does not rebuild | dedup_key `f"{run_id}:build"`; step ledger guards the handler body | tool test: two `runs.build` → same `object_id`; handler test: second dispatch does not call `builder.build` |
| failing build sets the Run `failed` with `build_failure` | builder raises `BUILD_FAILURE`; handler drives `running → failed` | handler test (builder raises): Run `failed`, `failure_category = build_failure`, job dead-lettered |

The issue's acceptance phrases the build-id clause as "build-id matches the **booted**
kernel," but #18 does not install or boot a kernel (out of scope, #19/#20). #18 is
falsifiable only up to "the `debuginfo_ref` is keyed by the produced `vmlinux`'s GNU
build-id"; the booted-kernel **match** (comparing that id to `/sys/kernel/notes` on the
running guest) is verified by the `live_vm` install/boot path (#19/#20) and consumed by
symbolization (#22/#24). Recorded so the deferred half is a decision, not a gap.

## 10. Risks / open questions

- **`runs.build` payload** carries **only `run_id`**. The handler re-reads the Run for
  the profile and the `system_id`, so the Run stays the single source of truth (no
  `system_id` or profile copied into the payload to drift from the Run row).
- **Shared file `runs.py`** is also touched by siblings; the build additions are
  localized (a new tool, a new handler, a `register_handlers`) and called out in
  NOTES.
- **`LockScope.RUN`** is additive (a new enum member); the global lock-order doc in
  ADR-0026 already names `RUN` last, so no reordering is needed.
