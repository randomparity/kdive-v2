# CLAUDE.md

This file provides guidance to coding agents when working with code in this repository.

## What this is

KDIVE (Kernel Debug, Inspect, Validate, Explore) is an MCP platform that gives agentic
coding environments a full Linux kernel build → boot → debug lifecycle across
heterogeneous resources (local VMs first; remote libvirt / cloud / bare-metal / PowerVM
planned). It is a greenfield rewrite of a single-user stdio PoC into a multi-user HTTP
service. Python 3.13, managed with `uv`.

Read `docs/specs/top-level-design.md` first — it is the authoritative architecture. The
current milestone plans are `docs/plans/m0-implementation.md` and
`docs/plans/m1-implementation.md`.

## Commands

The `justfile` is the **single source of truth** for build/lint/type/test commands. CI
(`.github/workflows/ci.yml`) and the pre-commit `ty` hook both invoke `just` recipes, so
run the same recipes locally rather than reinventing the underlying command:

| task | runs |
|------|------|
| `just setup` | check host deps, `uv sync --locked`, install + run git hooks |
| `just lint` | `ruff check` + `ruff format --check` |
| `just format` | `ruff check --fix` + `ruff format` (mutating) |
| `just type` | `ty check` — **whole tree (src + tests)**, not `src` alone |
| `just test` | the suite, excluding the gated `live_vm` marker |
| `just test-live` | the `live_vm` suite (needs a KVM/libvirt host + kdump guest image) |
| `just ci` | the full PR gate: lint, type, lint-shell, lint-workflows, check-mermaid, test |
| `just compose-up` / `compose-down` | Postgres + MinIO + mock-OIDC backing services for a live run |
| `just stack-up` | bring the live-stack backends up healthy + print host-process env (see runbook) |
| `just test-live-stack` | the `live_stack` suite; skips cleanly when the stack/fixtures are absent |

Run a single test: `uv run python -m pytest tests/mcp/test_allocations_tools.py::test_name -q`

`just type` is whole-tree on purpose: scoping `ty` to `src` once let a test-tree type
error merge green, so `tests/` is type-checked only here. Don't narrow it back.

## Host prerequisites

- `libvirt-dev` system headers — `libvirt-python` has no wheels and compiles against
  them; `uv sync` fails without them. CI apt-installs them; the README lists the distro
  command. `drgn` and `psycopg[binary]` need nothing extra.
- `just` and `prek` must be installed before `just setup` (it can't bootstrap its own
  runner): `uv tool install rust-just && uv tool install prek`.
- The db/integration tests need a reachable Docker daemon (disposable Postgres via
  testcontainers). They **skip** when Docker is absent — unless `KDIVE_REQUIRE_DOCKER=1`
  (set in CI), which turns the skip into a hard failure so a broken runner can't mask the
  schema tests.

## Architecture

### Three processes, one codebase

`python -m kdive {server|worker|reconciler}` (`src/kdive/__main__.py`):
- **server** — the FastMCP streamable-HTTP app; owns state machines, authz, admission
  control. Thin and fast; never blocks on a long provision.
- **worker** — pulls durable jobs from the Postgres-backed queue and runs provider
  operations. Long ops (provision/build/install/capture-vmcore) are jobs; the tool returns
  `{job_id, status: running}` and the agent polls `jobs.*`.
- **reconciler** — periodic drift-repair loop (ADR-0021): tears down orphaned Systems,
  fails Runs on torn-down Systems, reclaims expired leases, detaches dead DebugSessions.

State of record is **Postgres**; bulk artifacts (vmcores, transcripts) live in an
**S3-compatible object store**, referenced by row. Postgres advisory locks replace the
PoC's flock.

### Six durable objects

`Resource ──< Allocation ──< System ──< Run ──< DebugSession`, plus a cross-cutting
`Investigation` that groups Runs across Allocations/resource kinds. Each is a Postgres row
with an explicit state machine. Lower layers outlive higher ones; a System never outlives
its Allocation. See the design doc's "Domain model" section for the precise lifecycles.

### The plane / capability provider seam

This is the core extension mechanism (ADR-0009/0022). A provider implements only the
**planes** it supports (Discovery, Provisioning, Build, Install, Connect, Debug, Control,
Retrieve — eight Protocols in `providers/interfaces.py`; Allocation is core, not a plane).
The `CapabilityRegistry` (`providers/capability.py`) dispatches an operation to a provider
by matching `(plane, operation, resource_kind)` against advertised capabilities — **never
by provider name**. The registry is built once at startup and immutable.

Each operation declares an `OpContract` (`idempotent` / `destructive` / `cancelable` /
`long_running` / `cleanup`) that drives job routing, the destructive-op gate, and the
reconciler. The only provider today is `providers/local_libvirt/`.

The falsifiable design hypothesis: adding a new provider (e.g. remote libvirt in M2)
touches zero lines in the core and the MCP tool modules.

### Two registrar seams keep the entrypoint stable

`mcp/app.py` holds `_PLANE_REGISTRARS` (tools) and `_HANDLER_REGISTRARS` (worker job
handlers). A new plane appends to a tuple; `build_app` / `build_handler_registry` never
change. MCP tools (`mcp/tools/*.py`) are thin FastMCP wrappers over plain async handlers
that take an injected pool + `RequestContext`, so they are tested directly without a
transport.

### Cross-cutting invariants (apply on every plane)

- **Uniform response envelope** — every tool returns a `ToolResponse` (`mcp/responses.py`):
  object id, status, `suggested_next_actions` (literal next tool names), artifact `refs`,
  and an `error_category` **iff** the status is a failure (enforced at construction).
  References, never log dumps.
- **State transitions are guarded data** — `domain/state.py` is a nested adjacency table;
  the repository layer (`db/repositories.py`) calls `can_transition` before persisting any
  state change. An illegal edge raises `IllegalTransition` (a programming error, distinct
  from operational `ErrorCategory` failures).
- **Stable error taxonomy** — `domain/errors.py` `ErrorCategory`. Pick the most specific
  existing value; never invent strings.
- **Secrets by reference + mandatory redaction** — secrets resolve at the worker boundary
  and register into the redaction registry for the op's lifetime; only `(present,
  source-ref)` persists. All guest/console/gdb output passes the redactor before
  persistence or any response snippet (`security/`).
- **Destructive-op gate** — `security/gate.py`: power/force_crash/teardown/reprovision
  require all three of capability scope + RBAC role + explicit profile opt-in (deny by
  default).
- **Concurrency** — serialize per-Allocation and per-System via advisory locks; admission
  control's check-then-debit is atomic under a per-project lock. Idempotent steps keyed by
  `run_id` + step. The `tests/adversarial/` suite stress-tests these races.

## Conventions

- **Architecture decisions are ADRs** (`docs/adr/`, `NNNN-kebab-title.md`, monotonic
  numbers never reused). Don't change an accepted decision in place — write a new ADR that
  supersedes it. Most source modules cite the ADR(s) they implement in their docstring;
  follow the citation when changing behavior. Spec → plan → implementation cycles live
  under `docs/specs/`, `docs/plans/`, and `docs/superpowers/`.
- **Releasing** — see [`docs/RELEASING.md`](docs/RELEASING.md) and
  [ADR-0041](docs/adr/0041-versioning-release-process.md) (SemVer, milestone→minor,
  tag-driven release).
- **Doc-style guard** (enforced in CI / `check-mermaid` is mermaid-only, but the prose
  rule is project-wide): use **Milestone**, never "Sprint"; keep prose plain and factual —
  avoid "critical", "robust", "comprehensive", "elegant". This applies to ADRs, specs,
  commit messages, and code comments.
- **`live_vm` tests** are skipped by default (marker in `pyproject.toml`); they need an
  operator-provided KVM/nested-virt host with libvirt and a kdump-enabled guest image, and
  run only as a manually-dispatched self-hosted CI job. Unit/service tests depend only on
  disposable Postgres + MinIO + mock OIDC.
- **`live_stack` tests** drive the spine over the real MCP HTTP transport against a running
  host `server`/`worker`/`reconciler` + the compose backends; operator bring-up is in
  [`docs/runbooks/live-stack.md`](docs/runbooks/live-stack.md) (ADR-0042). `just
  test-live-stack` skips cleanly when the stack/fixtures (or the marked suite) are absent.
- Tests mirror the package tree under `tests/`; `tests/adversarial/` holds concurrency /
  property-based (hypothesis) race tests, `tests/integration/` holds the end-to-end
  milestone exercises.
- Ruff line length 100, lint set `E,F,I,UP,B,SIM`. `ty` runs with strict defaults (no
  project-wide relaxations); the unstubbed C-extension deps (`libvirt-python`, `drgn`)
  suppress `unresolved-import` with a scoped per-site ignore.
- Runtime env vars are `KDIVE_*` (`KDIVE_DATABASE_URL`, `KDIVE_OIDC_*`, `KDIVE_S3_*`,
  `KDIVE_HTTP_HOST/PORT`, `KDIVE_LOG_LEVEL`); see `docker-compose.yml` for a working set.
