# M0 — Walking Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement each sub-issue. Each
> sub-issue below is sized for a single PR; its bite-sized TDD steps are authored at
> execution time. Steps within an issue use checkbox (`- [ ]`) tracking.

**Goal:** Build the M0 walking skeleton from [`../specs/m0-walking-skeleton.md`](../specs/m0-walking-skeleton.md) — the thinnest real path through all nine planes on local libvirt/QEMU, on the new architecture.

**Architecture:** A thin async core (FastMCP/HTTP, state machines, admission, audit) over Postgres (system-of-record) + S3 object store, dispatching long-running work to a Postgres-backed job queue and a worker tier. Providers implement narrow typed runtime ports; M0 ships exactly one provider (local-libvirt). ADR-0063 supersedes the early capability-registry dispatch plan for production runtime assembly. Salvaged v1 modules (redaction, paths, gdb-MI, drgn, crash, error taxonomy) are ported behind the new seams.

**Tech Stack:** Python 3.13 · `uv` · FastMCP 3.x (ADR-0010) · Postgres via `psycopg` 3 (async) · S3 via `boto3` · `libvirt-python` · `drgn` · Pydantic v2 · `ruff`/`ty`/`pytest` · `prek`.

---

## GitHub mechanics

- **Epic:** one parent issue (`type:epic`, milestone `M0 — Walking skeleton`), with each task below attached as a **native GitHub sub-issue**.
- **Labels:** reuse the existing taxonomy (`type:*`, `area:*`, `provider:*`); add `type:epic`.
- **Milestone:** `M0 — Walking skeleton` (exists).
- Each sub-issue carries its `area:*` (and `provider:local-libvirt` where it touches the provider), a `Depends on:` line, and acceptance criteria copied into the issue body.

## Package layout

```
src/kdive/
  domain/      models.py · state.py · errors.py
  db/          schema/NNNN_*.sql · migrate.py · pool.py · repositories.py · locks.py · idempotency.py
  store/       objectstore.py
  jobs/        models.py · queue.py · worker.py
  mcp/         app.py · auth.py · responses.py
  mcp/tools/   catalog/{resources,jobs,investigations,artifacts*}.py
               lifecycle/{allocations,control,vmcore}.py
               lifecycle/{runs,systems}/
               debug/{sessions,ops,introspect}.py
               accounting/{admin,estimate,reports,usage}.py
               ops/{audit,breakglass,inventory,queue,reconcile,tuning}.py
  jobs/handlers/{runs.py · systems.py · control.py · vmcore.py}
  services/    allocation_admission.py · allocation_idempotency.py · resource_discovery.py
  security/    rbac.py · audit.py · gate.py · redaction.py · secret_registry.py · paths.py · secrets.py
  reconciler/  loop.py
  providers/   composition.py · ports/ · local_libvirt/{build,discovery,retrieve}.py
               local_libvirt/lifecycle/{provisioning,install,connect,control}.py
               local_libvirt/debug/{debug_gdbmi,introspect_drgn,execution,mi_*}.py
  profiles/    provisioning.py · build.py
  log.py       structured-logging config (JSON, stdlib logging)
  __main__.py  (server | worker | reconciler entrypoints)
tests/         mirrors src/kdive/ ; tests/integration/ for the live_vm path
```

## Dependency graph (issue order)

```
0: #1 scaffolding → #2 CI
1 (core, gating path): #3 domain → #4 schema → #5 repo/locks → #6 store
                          #5 → #7 jobs → #8 mcp/auth → #9 rbac/audit/gate → #10 reconciler
2: #11 provider framework  (needs #3, #5)
3 (planes, behind #11; "←" = depends on):
   #13 profile(←#3) · #12 discovery+alloc(←#11,#9) · #14 provision(←#11,#13)
   #15 run/investigation(←#5,#8) → #16 build(←#11,#15) → #17 install/boot(←#16,#14)
   #18 connect(←#11,#17,#23) → #19 gdb-MI(←#18)
   #21 control(←#11,#14,#9) → #22 retrieve(←#21,#6,#23) → #20 drgn-from-vmcore(←#22)
   #23 safety/secrets(←#1) — Phase-1 foundation, needed by #18 and #22 (redaction)
4: #24 end-to-end integration (needs #1–#23)
```

The core platform (#3–#10) is the gating path; planes parallelize once #11 lands.

## Test environments

- **Unit + service tests** run anywhere: a disposable Postgres + MinIO + a mock OIDC issuer (the compose file from #24) — used by #4–#11, #13, #15, #20, #23.
- **`live_vm` tests** (#14, #17, #18, #19, #21, #22 acceptance) require a **KVM/nested-virt-capable host** with libvirt and a **kdump-enabled guest image** (`crashkernel=` reservation + kdump service). They run on a self-hosted KVM runner or as a manual pre-merge gate — **not** on stock GitHub-hosted runners. #1 documents the runner prerequisites; CI marks `live_vm` as a separate, manually-triggered job.
- **Kernel builds** (#16, and #24's build step) need a kernel **toolchain** (gcc/clang, make, bc, flex, bison, libelf-dev) and a **warm kernel source tree** in the build workspace. M0 builds **incrementally** from the warm tree (minutes), not from scratch (tens of minutes); #1's runner docs name the source location and ccache/workspace path.
- **Fixtures are repo-owned and reproducible**, not hand-built: #24 provides `scripts/live-vm/build-guest-image.sh` (a kdump+`crashkernel=` guest image via mkosi/virt-builder) and `scripts/live-vm/fetch-kernel-tree.sh` (clone/cache the kernel source at `$KDIVE_KERNEL_SRC`). A `live_vm` **preflight** fails fast with a clear message if the image or tree is absent, so a missing fixture is a typed error, not a confusing mid-test failure.

---

## Phase 0 — Repo setup

### Issue 1 — Repo scaffolding & tooling
- **Labels:** `type:chore` · `area:core-platform`
- **Depends on:** —
- **Goal:** A `uv`-managed Python 3.13 project that lints, type-checks, and runs an empty test suite green.
- **Files:** Create `pyproject.toml`, `uv.lock`, `src/kdive/__init__.py`, `tests/__init__.py`, `tests/test_smoke.py`, `.gitignore`, `.pre-commit-config.yaml`, `README.md` (update).
- **Scope:**
  - `pyproject` with `requires-python==3.13`, pinned deps (FastMCP, psycopg[binary], boto3, libvirt-python, drgn, pydantic), `[tool.ruff]`, `[tool.ty.rules]`, `[tool.pytest.ini_options]` (incl. a `live_vm` marker).
  - `src/` layout per "Package layout"; create empty `__init__.py` for each package.
  - `.gitignore` includes `*.swp`, `.venv/`, `__pycache__/`, `*.pyc`, `.ruff_cache/`.
  - prek hooks: ruff, ruff-format, ty, end-of-file/trailing-whitespace.
  - Document the `live_vm` test-runner prerequisites (KVM/nested-virt host, libvirt, kdump-enabled guest image) in `README.md` (see "Test environments").
  - Structured-logging foundation: a `kdive/log.py` configuring stdlib `logging` for JSON/key-value output (context: request id, job id, principal, object id, transition) — no new dependency. Server, worker, and reconciler initialize it; later issues emit through it.
- **Acceptance:** `uv sync` succeeds; `uv run ruff check .` and `uv run ty check` pass; `uv run pytest -q` runs `test_smoke.py` green; `prek run -a` passes.

### Issue 2 — CI workflow & Dependabot
- **Labels:** `type:chore`
- **Depends on:** #1
- **Goal:** CI runs lint/type/test on PRs; mermaid docs are parse-checked; Dependabot configured.
- **Files:** Create `.github/workflows/ci.yml`, `.github/workflows/docs-mermaid.yml`, `.github/dependabot.yml`.
- **Scope:**
  - `ci.yml`: matrix-free job on `ubuntu-latest`, `uv sync`, ruff/ty/`pytest -m "not live_vm"`; the `live_vm` suite is a separate, manually-triggered job on the self-hosted KVM runner; actions pinned to SHA with version comments; `persist-credentials: false`.
  - `docs-mermaid.yml`: extract ` ```mermaid ` blocks and `mermaid.parse()` them via jsdom (the browserless check proven in this repo) so diagram errors fail CI.
  - `dependabot.yml`: `uv` + `github-actions` ecosystems, 7-day cooldown, grouped.
- **Acceptance:** a test PR shows all three checks; an intentionally broken mermaid block fails `docs-mermaid`; `actionlint`/`zizmor` clean.

---

## Phase 1 — Core platform (the gating path)

### Issue 3 — Domain models & error taxonomy
- **Labels:** `area:core-platform`
- **Depends on:** #1
- **Goal:** Typed models for the six durable objects with M0-subset state machines, and the ported `ErrorCategory`.
- **Files:** Create `src/kdive/domain/models.py`, `domain/state.py`, `domain/errors.py`; tests under `tests/domain/`.
- **Scope:**
  - Port `~/src/kdive-v1/src/kdive/domain.py` `ErrorCategory` and extend with the M0 categories from the spec (`allocation_denied`, `lease_expired`, `provisioning_failure`, `install_failure`, `transport_failure`, `control_failure`).
  - Pydantic models: Resource, Allocation, System, Investigation, Run, DebugSession, Job, Artifact — fields per the spec's schema section; `external_refs` on Investigation.
  - `state.py`: `StrEnum` per object + a `can_transition(frm, to)` guard table encoding the M0 state machines (incl. System `crashed`).
- **Acceptance:** unit tests assert every legal transition passes and a representative illegal one raises; `ErrorCategory` round-trips to/from its stable string values.

### Issue 4 — Postgres schema & migration runner
- **Labels:** `area:core-platform`
- **Depends on:** #3
- **Goal:** The M0 schema applied by a minimal forward-only migration runner.
- **Files:** Create `src/kdive/db/schema/0001_init.sql`, `db/migrate.py`, `db/pool.py`; `tests/db/test_migrate.py`.
- **Scope:**
  - `0001_init.sql`: the tables from the spec (resources, allocations, systems, investigations[+`external_refs jsonb`], runs[+`kernel_ref`, `debuginfo_ref`], run_steps[`UNIQUE(run_id,step)`], debug_sessions, jobs[`dedup_key NOT NULL UNIQUE`], artifacts, audit_log) with the attribution columns and FKs.
  - `migrate.py`: applies `schema/NNNN_*.sql` in order, tracked in a `schema_migrations` table; idempotent re-run.
  - `pool.py`: async `psycopg_pool.AsyncConnectionPool` from env (`KDIVE_DATABASE_URL`).
- **Acceptance:** against a disposable Postgres (testcontainers or a CI service), `migrate.py` creates all tables; re-running is a no-op; `\d` shows the unique constraints.

### Issue 5 — Repository layer, advisory locks, idempotency ledger
- **Labels:** `area:core-platform`
- **Depends on:** #4
- **Goal:** Typed CRUD over each object, per-Allocation/per-System serialization, and idempotent step execution.
- **Files:** Create `src/kdive/db/repositories.py`, `db/locks.py`, `db/idempotency.py`; `tests/db/`.
- **Scope:**
  - `repositories.py`: async insert/get/update-state for each object, returning `domain.models`; state updates go through `domain.state.can_transition`.
  - `locks.py`: `async with advisory_xact_lock(conn, scope, key)` using `pg_advisory_xact_lock` (ADR-0005 — transaction scope, pooler-safe).
  - `idempotency.py`: `run_step(run_id, step, fn)` — returns the stored `result` if the `(run_id, step)` row exists, else runs `fn`, stores, returns.
- **Acceptance:** concurrent transactions contend correctly on the advisory lock (one blocks until the other commits, proven with two connections); a re-run of `run_step` does not re-execute `fn` (assert call count == 1).

### Issue 6 — Object-store client
- **Labels:** `area:core-platform`
- **Depends on:** #4
- **Goal:** S3-compatible artifact storage with the spec's key scheme, sensitivity/retention, and write-before-commit ordering.
- **Files:** Create `src/kdive/store/objectstore.py`; `tests/store/test_objectstore.py`.
- **Scope:**
  - `put_artifact(tenant, kind, object_id, name, data, sensitivity, retention) -> (key, etag)`; key = `{tenant}/{kind}/{object_id}/{name}`.
  - `get_artifact(key, etag)` raises `stale_handle` (ErrorCategory) on etag mismatch/missing.
  - Helper `register_artifact_row(...)` ordering note: object written first, row committed by the caller (#22) after.
- **Acceptance:** against MinIO (testcontainers), put then get round-trips; a get with a stale etag raises `stale_handle`; sensitivity tag is persisted as object metadata.

### Issue 7 — Job queue & worker tier
- **Labels:** `area:core-platform`
- **Depends on:** #5
- **Goal:** Durable jobs with at-least-once delivery, lease/heartbeat, bounded retries, and admission idempotency.
- **Files:** Create `src/kdive/jobs/models.py`, `jobs/queue.py`, `jobs/worker.py`; `tests/jobs/`.
- **Scope:**
  - `queue.enqueue(kind, payload, authorizing, dedup_key)` — upsert-then-fetch so a re-issue returns the **existing** job (admission idempotency): `INSERT ... ON CONFLICT (dedup_key) DO NOTHING` followed by `SELECT id, status FROM jobs WHERE dedup_key = $1` in the same transaction. (`DO NOTHING RETURNING` returns no row on conflict, so it cannot return the existing job — do not use it.)
  - `queue.dequeue()` — `SELECT ... FOR UPDATE SKIP LOCKED`, set `worker_id`/`lease_expires_at`.
  - `worker.run(handlers)` — claim, heartbeat, dispatch by `kind`, on success store `result_ref`; on exception increment `attempt`, requeue or dead-letter to `failed` past `max_attempts`.
  - A `JobHandler` registry keyed by kind (handlers registered by the plane issues).
  - **Every long-running tool computes a `dedup_key`** so retries never double-enqueue: run-scoped jobs use `(run_id, step, kind)`; System-scoped jobs use a scope-appropriate key — `(allocation_id, "provision")`, `(system_id, "teardown")`, `(system_id, "capture_vmcore")`, `(system_id, "force_crash")`, `(system_id, "power", action)`. `dedup_key` is `NOT NULL`.
- **Acceptance:** enqueue+dequeue+complete happy path; a re-enqueue with the same `dedup_key` returns the same `job_id` (no duplicate); a handler that always raises dead-letters at `max_attempts`; a lapsed lease (simulated) returns the job to the queue.

### Issue 8 — FastMCP/HTTP skeleton + OIDC auth + jobs.* tools
- **Labels:** `area:mcp-api` · `area:security`
- **Depends on:** #5, #7
- **Goal:** A FastMCP streamable-HTTP server that authenticates bearer JWTs and exposes the `jobs.*` tools.
- **Files:** Create `src/kdive/mcp/app.py`, `mcp/auth.py`, `mcp/responses.py`,
  `mcp/tools/catalog/jobs.py`, `src/kdive/__main__.py`; `tests/mcp/`.
- **Scope:**
  - `auth.py`: FastMCP `JWTVerifier` against `KDIVE_OIDC_JWKS_URI`, enforcing `iss` + `aud` (ADR-0002/0010); derive `principal` (sub), `agent_session` (claim, optional in M0), and validate `project` against the request param.
  - `app.py`: `FastMCP(name=..., auth=...)`, `transport="http"`; a request-context
    accessor returning `(principal, agent_session, project)`. **`app.py` aggregates the
    tool surface** through `_PLANE_REGISTRARS` and `_HANDLER_REGISTRARS`, so each nested
    tool or worker module contributes one registrar without changing `build_app` or
    `build_handler_registry`.
  - `tools/jobs.py`: `jobs.get/.wait/.cancel/.list` returning the spec's job-handle shape.
  - `__main__.py`: `server` and `worker` subcommands — `server` runs the app; `worker` runs the `worker.run` loop from #7 (the process that executes jobs). Both initialize the `kdive/log.py` structured logger (#1) and emit per-request/per-job logs keyed by id + principal.
- **Acceptance:** a request with no/invalid token is rejected; a valid token resolves the principal context; `jobs.get` on a known job returns its status; structured JSON shape matches the spec (object id, status, suggested_next_actions, refs).

### Issue 9 — RBAC, audit log, destructive-op gate
- **Labels:** `area:security`
- **Depends on:** #5, #8
- **Goal:** Project-scoped roles, append-only audit on every transition, and the three-check destructive gate.
- **Files:** Create `src/kdive/security/authz/rbac.py`, `security/audit.py`, `security/authz/gate.py`; `tests/security/`.
- **Scope:**
  - `authz/rbac.py`: `viewer`/`operator`/`admin` per project from token claims; `require_role(ctx, project, role)`.
  - `audit.py`: `record(ctx, tool, object_kind, object_id, transition, args)` → append-only `audit_log` row with `args_digest` (hash, not raw args).
  - `authz/gate.py`: `assert_destructive_allowed(ctx, allocation, op)` — all three of capability scope, `admin` role, explicit profile opt-in (ADR-0006).
- **Acceptance:** a destructive op is refused if any single check is absent (three tests); every state transition through a repository writes exactly one audit row; `args_digest` never contains secret material.

### Issue 10 — Reconciler loop (M0 subset)
- **Labels:** `area:core-platform`
- **Depends on:** #5, #7
- **Goal:** Periodic drift repair for the M0 failure cases.
- **Files:** Create `src/kdive/reconciler/loop.py`; add `reconciler` subcommand to `__main__.py`; `tests/reconciler/`.
- **Scope:**
  - Orphaned System (Allocation `released`/`failed`) → teardown job; abandoned job (lapsed lease past `max_attempts`) → `failed` + compensation; dead DebugSession (`live`, stale heartbeat) → `detached`; leaked libvirt domain via provider `list_owned` (tagged `system_id` with no live row, outside the provision grace window).
  - Lease-expiry policy: drain grace window → force-kill → Run `failed` (`lease_expired`).
  - Each repair emits a structured log line (object kind/id + action taken) via `kdive/log.py` (#1), so drift events are observable.
- **Acceptance:** seeded drift rows are repaired on one loop pass (one test per case); a domain mid-provision (job in-flight) is **not** reaped.

### Issue 23 — Port safety modules + file-ref secret backend
- **Labels:** `area:security`
- **Depends on:** #1
- **Goal:** Redaction, path-safety, and a by-reference secret backend, ported and wired. (Phase-1 foundation — the debug/retrieve planes depend on it for transcript/vmcore redaction.)
- **Files:** Create `src/kdive/security/secrets/redaction.py`, `security/secrets/secret_registry.py`, `security/secrets/paths.py`, `security/secrets/secrets.py` (port `~/src/kdive-v1/src/kdive/safety/{redaction,secret_registry,paths}.py`); tests.
- **Scope:**
  - Port the three safety modules behind the new package; keep `PROCESS_SECRET_REGISTRY` semantics.
  - `secrets/secrets.py`: `SecretBackend` Protocol + `FileRefBackend` resolving only within an allowlisted root (path-safety check), registering the value into the redaction registry **before** use; pre-registration output quarantined.
- **Acceptance:** a registered secret value is masked by exact-value replacement in sample output; a file-ref escaping the allowlisted root is rejected; resolution registers before returning the value (ordering test).

---

## Phase 2 — Provider framework

### Issue 11 — Historical capability registry prototype and plane interfaces
- **Labels:** `area:providers`
- **Depends on:** #3, #5
- **Goal:** The original provider-seam prototype — typed plane `Protocol`s, an `OpContract`,
  and capability-match dispatch (never by name). ADR-0063 later superseded this for
  production runtime assembly with typed `ProviderRuntime` ports, and ADR-0066 removed the
  registry prototype from production source.
- **Files:** `providers/ports/`; historical registry design remains in ADR-0009,
  ADR-0022, and dated planning docs.
- **Scope:**
  - `ports/`: provider-plane `Protocol`s and value aliases used by `ProviderRuntime`
    typed ports, with the package facade as the stable import surface. Allocation is
    handled in core, not as a provider Protocol.
- **Acceptance:** live provider tests cover typed runtime composition; no production
  capability-registry API remains.

---

## Phase 3 — Planes (local-libvirt)

These issue recipes are historical M0 slices, but the file paths below name the
current runtime layout. Provider work should extend typed ports wired by
`providers/composition.py`; the removed capability-registry dispatch path is
only ADR history.

### Issue 12 — Discovery + Allocation (admission)
- **Labels:** `area:allocation` · `provider:local-libvirt`
- **Depends on:** #11, #9
- **Goal:** Register the local libvirt host and grant always-yes, capacity-checked allocations.
- **Files:** Create `src/kdive/providers/local_libvirt/discovery.py`,
  `src/kdive/mcp/tools/catalog/resources.py`, `mcp/tools/lifecycle/allocations.py`,
  `src/kdive/services/allocation_admission.py`; tests.
- **Scope:**
  - `discovery.py`: enumerate the libvirt host (`libvirt.open(KDIVE_LIBVIRT_URI)`), advertise arch/cpu/memory + `gdbstub` transport capability; `list_owned()` returns domains tagged with `system_id`.
  - `allocation_admission.py`: per-project advisory lock → check per-host concurrent-Allocation cap → grant or `allocation_denied`.
  - tools: `resources.list/.describe`, `allocations.request/.get/.release/.list`.
- **Acceptance:** `resources.list` returns the host with capabilities; `allocations.request` grants under cap and denies (`allocation_denied`) at cap; release transitions to `released` and audit rows are written.

### Issue 13 — Provisioning-profile schema
- **Labels:** `area:provisioning`
- **Depends on:** #3
- **Goal:** The ADR-0011 declarative profile with a libvirt variant.
- **Files:** Create `src/kdive/profiles/provisioning.py`; `tests/profiles/`.
- **Scope:**
  - Pydantic `ProvisioningProfile` with provider-agnostic core (arch, vcpu, memory, disk, boot_method, kernel_source_ref) + `provider: {local-libvirt: LibvirtProfile{domain_xml_params, rootfs_image_ref, crashkernel}}`; versioned; rejects unknown fields (`configuration_error`).
  - Core validates agnostic fields; provider section validated by its own model.
- **Acceptance:** a valid libvirt profile parses; a missing required field raises `configuration_error`; the `crashkernel` field is present (kdump prerequisite).

### Issue 14 — Provisioning plane (libvirt)
- **Labels:** `area:provisioning` · `provider:local-libvirt`
- **Depends on:** #11, #13
- **Goal:** Provision and tear down a libvirt System as a job.
- **Files:** Create `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`,
  `src/kdive/mcp/tools/lifecycle/systems/`, `src/kdive/jobs/handlers/systems.py`; register
  `provision`/`teardown` job handlers; tests.
- **Scope:**
  - Create the `systems` row (`provisioning`) **first**, then render domain XML from the profile + rootfs and `defineXML`/`create`, tagging the domain with `system_id` metadata (ADR-0009/reconciler ordering).
  - `teardown`: `destroy`+`undefine`; idempotent.
  - tools: `systems.provision` (→ job, `dedup_key=(allocation_id, "provision")`), `systems.get`, `systems.teardown` (→ job, `dedup_key=(system_id, "teardown")`).
- **Acceptance (live_vm):** `systems.provision` drives `defined → provisioning → ready` and a libvirt domain exists tagged with the `system_id`; `teardown` removes it; the System never outlives its Allocation (reconciler test from #10 still holds).

### Issue 15 — Investigation + Run lifecycle & tools
- **Labels:** `area:core-platform`
- **Depends on:** #5, #8
- **Goal:** Campaign + run-attempt lifecycle, including external references.
- **Files:** Create `src/kdive/mcp/tools/catalog/investigations.py`,
  `mcp/tools/lifecycle/runs/`; `tests/`.
- **Scope:**
  - `investigations.open(project,title,external_refs?)`, `.get`, `.close`, `.link`, `.unlink` (mutable `external_refs` of `{tracker,id,url}`); `open → active` on first Run.
  - `runs.create(investigation_id, system_id, build_profile)`, `.get`; Run state machine; a failed step is terminal for the Run (Failure & retry section).
  - Binding invariant enforced: `run.system → allocation`.
- **Acceptance:** open→link→unlink mutate `external_refs`; creating the first Run flips the Investigation to `active`; creating a Run on a torn-down System is rejected (`stale_handle`).

### Issue 16 — Build plane (local make)
- **Labels:** `area:build-install`
- **Depends on:** #11, #15
- **Goal:** Build a kernel from source as an idempotent job.
- **Files:** Create `src/kdive/providers/local_libvirt/build.py`,
  `src/kdive/profiles/build.py`, `mcp/tools/lifecycle/runs/build.py`; register
  `build` handler in `src/kdive/jobs/handlers/runs.py`; tests.
- **Scope:**
  - `BuildProfile` (kernel source ref, config, patch ref); `build()` runs `make` in a workspace, ensuring `CONFIG_CRASH_DUMP`/`crashkernel` + `CONFIG_DEBUG_INFO(_DWARF)`/BTF for the kdump and symbolization prerequisites; stores **two** artifacts on the Run — the bootable kernel image (`kernel_ref`) **and a build-id-keyed `vmlinux`/debuginfo artifact (`debuginfo_ref`)** that #20/#22 load to symbolize the vmcore (port v1 `symbols/`).
  - To bound wall-clock, M0 builds **incrementally from a warm source tree** (base tree + the profile's patch), not a cold from-scratch build; the warm tree is part of the build-workspace setup.
  - Step keyed `(run_id, "build")` via the idempotency ledger.
- **Acceptance:** a build job produces a kernel image **and** a debuginfo artifact whose build-id matches the booted kernel, and sets `kernel_ref`/`debuginfo_ref`; a re-issued `runs.build` returns the same job (dedup) and does not rebuild; a failing build sets the Run `failed` with `build_failure`.

### Issue 17 — Install + boot plane
- **Labels:** `area:build-install` · `provider:local-libvirt`
- **Depends on:** #16, #14
- **Goal:** Install the built kernel onto the System and boot it, with kdump in effect.
- **Files:** Create `src/kdive/providers/local_libvirt/lifecycle/install.py`,
  `mcp/tools/lifecycle/runs/steps.py` (add `runs.install`, `runs.boot`); register
  handlers in `src/kdive/jobs/handlers/runs.py`; tests.
- **Scope:**
  - `install()`: stage kernel/initrd for direct-kernel boot with a `crashkernel=` reservation; ensure the kdump capture service/initramfs is present (kdump prerequisite).
  - `boot()`: boot the installed kernel; run-readiness preflight (port v1 `prereqs/`) before declaring boot ready; `boot_timeout`/`readiness_failure` on failure.
- **Acceptance (live_vm):** install+boot brings the System up on the built kernel; readiness preflight passes; a kernel without `crashkernel=` is rejected at install (`configuration_error`).

### Issue 18 — Connect plane (gdbstub) + DebugSession lifecycle
- **Labels:** `area:debug` · `provider:local-libvirt`
- **Depends on:** #11, #17, #23
- **Goal:** Open a single-attach gdbstub transport and manage the DebugSession row.
- **Files:** Create `src/kdive/providers/local_libvirt/lifecycle/connect.py`,
  `mcp/tools/debug/sessions.py` (start/end session); port
  `~/src/kdive-v1/.../transport/backends/qemu_gdbstub.py`; tests.
- **Scope:**
  - `open_transport(system, "gdbstub")` → QEMU gdbstub; `debug.start_session` inserts a `debug_sessions` row `attach → live` with the transport handle + heartbeat; a second attach to a `live` session returns `transport_conflict`.
  - `debug.end_session` → `detached`; `force_crash`/reboot also drives `live → detached` (coordinated with #21).
- **Acceptance (live_vm):** start_session attaches and the row is `live`; a second start_session returns `transport_conflict`; end_session detaches.

### Issue 19 — Debug plane: port gdb-MI tier
- **Labels:** `area:debug`
- **Depends on:** #18
- **Goal:** Constrained debug ops over the gdbstub via the ported gdb-MI tier.
- **Files:** Create `src/kdive/providers/local_libvirt/debug/debug_gdbmi.py` plus
  sibling MI protocol/controller/execution/transcript helpers as needed; add to
  `mcp/tools/debug/ops.py`; tests.
- **Scope:**
  - Port gdb-MI behind the typed `GdbMiEngine` runtime port; expose `debug.set_breakpoint/.clear/.list`, `.read_memory` (enforce the **4096-byte cap**, ported invariant), `.read_registers`, `.continue`, `.interrupt`.
  - All transcript output passes through the redactor (#23) before persistence/response.
- **Acceptance (live_vm):** set a breakpoint at a symbol, continue, hit it, read registers and ≤4096 bytes of memory; a >4096 read is rejected; a known secret in the gdb-MI **textual transcript** is masked in the response (this is transcript redaction — raw `read_memory` bytes are returned verbatim under the 4096-byte cap, not redacted).

### Issue 20 — Debug plane: drgn introspection from vmcore (offline)
- **Labels:** `area:debug`
- **Depends on:** #22
- **Goal:** Offline drgn introspection of a captured vmcore on the host — no live guest, no SSH.
- **Files:** Create `src/kdive/providers/local_libvirt/debug/introspect_drgn.py` (port the M0 subset of the vmcore path in `~/src/kdive-v1/src/kdive/introspect/*` + `prereqs/drgn_probe.py`); add `introspect.from_vmcore`; tests.
- **Scope:**
  - drgn opens the fetched vmcore on the host (`drgn.program_from_core`-style), **loading the Run's `debuginfo_ref` (`vmlinux`) from #16 for symbols/types**, and runs a minimal helper set (tasks, modules, sysinfo).
  - **Live drgn (`introspect.run`) is deferred to M1.** v1 implements it as drgn-over-SSH (`local-drgn-introspect`, `transports=["ssh"]`), which needs a guest SSH transport + credentials (secret backend) that the M0 walking-skeleton path does not otherwise require; introducing it here is scope creep and `introspect.run` is not in the M0 tool subset.
  - Output redacted before persistence.
- **Acceptance:** `introspect.from_vmcore` returns task/module/sysinfo data from a captured vmcore; output is redacted. Runs offline against the artifact from #22 — no `live_vm` host needed.

### Issue 21 — Control plane: power + force_crash (gated)
- **Labels:** `area:control-retrieve` · `provider:local-libvirt`
- **Depends on:** #11, #14, #9
- **Goal:** Power/reset and force_crash, behind the destructive-op gate.
- **Files:** Create `src/kdive/providers/local_libvirt/lifecycle/control.py`,
  `mcp/tools/lifecycle/control.py`, `src/kdive/jobs/handlers/control.py`; register
  `force_crash`/power job handlers; tests.
- **Scope:**
  - `power(on|off|cycle|reset)` via `virsh`/libvirt API; `force_crash` via `sysrq-c` (or QEMU monitor `nmi`), driving System `ready → crashed` and the DebugSession `live → detached`.
  - Every op passes `gate.assert_destructive_allowed` (#9); jobs use `dedup_key=(system_id, op[, action])`.
- **Acceptance:** `force_crash` is refused without admin/scope/opt-in (gate test); (live_vm) force_crash panics the guest and transitions System+DebugSession correctly.

### Issue 22 — Retrieve plane: vmcore capture/fetch + crash postmortem
- **Labels:** `area:control-retrieve`
- **Depends on:** #21, #6, #23
- **Goal:** Capture the kdump vmcore, store it (raw + redacted), and port crash postmortem.
- **Files:** Create `src/kdive/providers/local_libvirt/retrieve.py` (port subset of
  `~/src/kdive-v1/.../postmortem/*`), `mcp/tools/lifecycle/vmcore.py`,
  `mcp/tools/catalog/artifacts.py`, `src/kdive/jobs/handlers/vmcore.py`; register
  `capture_vmcore` handler; tests.
- **Scope:**
  - `capture_vmcore` waits for kdump to finish, writes the **raw** vmcore (`sensitive`) and a **redacted** derivative to the object store (object before row, #6), returns an artifact ref; if no core within the capture window → `readiness_failure`. The job is enqueued with `dedup_key=(system_id, "capture_vmcore")`.
  - `vmcore.list/.fetch` (→ job), `artifacts.list/.get` (returns the redacted derivative only); crash postmortem port for `postmortem.crash`/`.triage`, loading the Run's `debuginfo_ref` (from #16) to symbolize the core.
- **Acceptance (live_vm):** after force_crash, `vmcore.fetch` produces a fetchable vmcore artifact; `artifacts.get` returns the redacted derivative; a no-core scenario returns `readiness_failure`.

---

## Phase 4 — Integration

### Issue 24 — End-to-end walking-skeleton integration test
- **Labels:** `type:test` · `area:core-platform`
- **Depends on:** #1–#23 (the full stack — notably #9 gate and #10 reconciler, which the acceptance below exercises)
- **Goal:** Prove the six exit criteria end-to-end on a real libvirt host.
- **Files:** Create `tests/integration/test_walking_skeleton.py` (marker `live_vm`); `scripts/live-vm/build-guest-image.sh` and `scripts/live-vm/fetch-kernel-tree.sh` (reproducible fixtures) plus a `live_vm` preflight that fails fast if the image/tree is absent; a `docker-compose`/`justfile` for Postgres + MinIO + a mock OIDC issuer.
- **Scope:** Drive the full path: `allocations.request → investigations.open(external_refs) → systems.provision → runs.create → build → install → boot → debug.start_session → set_breakpoint/read_memory → force_crash → vmcore.fetch → artifacts.get → allocations.release`.
- **Acceptance:** the spec's six exit criteria each have an assertion — path completes; every transition + force_crash wrote an audit row; a planted secret is redacted; replaying a completed step doesn't re-execute; release tears down with no orphaned domain; force_crash is refused when a gate check is absent.

---

## Self-review (spec coverage)

- Walking-skeleton path → #12,#14,#15,#16,#17,#18,#19,#21,#22,#24. ✓
- Six durable objects + state machines → #3; schema → #4; locks/idempotency → #5. ✓
- Job queue/worker (lease, dead-letter, dedup) → #7. ✓
- Object store (layout, sensitivity, ordering) → #6,#22. ✓
- MCP tool surface + FastMCP/auth → #8,#12,#14,#15,#19,#21,#22. ✓
- Plane interfaces + historical capability-dispatch prototype → #11; active runtime seam → ADR-0063. ✓
- Local-libvirt provider per plane → #12,#14,#16,#17,#18,#19,#20,#21,#22. ✓
- Auth/RBAC/attribution + destructive gate → #8,#9. ✓
- Cross-cutting redaction/secrets/audit → #9,#23. ✓
- Reconciler subset + lease-expiry → #10. ✓
- Error taxonomy → #3 (used throughout). ✓
- Ported PoC modules → #3 (domain),#19 (gdb-MI),#20 (drgn),#22 (crash),#23 (safety),#17 (preflight). ✓
- Exit criteria → #24. ✓

No spec section is unmapped.
