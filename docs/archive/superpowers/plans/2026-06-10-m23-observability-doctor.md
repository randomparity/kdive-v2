# M2.3 — Observability & doctor Implementation Plan

> **For agentic workers:** this is a **milestone plan** — a sequenced set of eight
> issue briefs to be filed as a GitHub epic + sub-issues and implemented one each
> via the `work-issue` skill (TDD + adversarial-review loops live *inside* each
> `/work-issue` run, as in M1.3–M2.2). This document is the per-issue scope /
> file-map / acceptance contract, not the bite-sized TDD steps. Steps use checkbox
> (`- [ ]`) syntax for tracking the issues.

**Goal:** Make kdive operable by someone other than its author — add OpenTelemetry
logs/metrics/traces + service health endpoints across all three processes, and a
`kdivectl doctor` preflight that names the exact fix for the four M2 contract faults.

**Architecture:** Two tracks sharing only the `KDIVE_*` config seam. *Telemetry &
health* adopts OTel as the signal spine (logs migrate onto it, metrics/traces are
net-new) behind a `kdive/observability/` facade, and adds a loopback-bound aux HTTP
listener (`/livez` `/readyz` `/metrics`) on all three processes. *Diagnostics &
doctor* is a server-side, authz-gated MCP tool whose checks run each from its correct
vantage, surfaced as `kdivectl doctor`.

**Tech Stack:** Python 3.13 · `uv`/`ruff`/`ty`/`pytest` · OpenTelemetry SDK + OTLP
exporters · FastMCP (existing) · the M1.5 `fault_inject` mock provider for CI · the
`KDIVE_*` config registry (ADR-0087).

**Specs/ADRs:** [`../specs/2026-06-10-m23-observability-doctor-design.md`](../specs/2026-06-10-m23-observability-doctor-design.md)
· ADR-0090 (OTel adoption / health) · ADR-0091 (doctor / diagnostics model).

---

## Grounding facts (verified against the tree)

- **Process entrypoints** are `python -m kdive {server|worker|reconciler}` in
  `src/kdive/__main__.py` (`_run_server`, `_run_worker`, `_run_reconciler`); all three
  call the logging configurator at startup. Logging lives in `src/kdive/log.py`
  (ADR-0014: stdlib JSON, `bind_context`, `SecretRedactionFilter`).
- **Server** is the FastMCP streamable-HTTP app (`src/kdive/mcp/app.py`,
  middleware in `src/kdive/mcp/middleware.py`). **Worker** is `src/kdive/jobs/worker.py`;
  **reconciler** is `src/kdive/reconciler/loop.py` — both plain asyncio loops, no HTTP.
- **Config registry (ADR-0087)** is `src/kdive/config/{registry,manifest,core_settings}.py`;
  new `KDIVE_OTEL_*` / health keys register here and flow into the generated reference.
- **Ops MCP tools** live in `src/kdive/mcp/tools/ops/` (e.g. `breakglass.py`,
  `secrets.py`); the platform-role gate helper is `ops/_auth.py`. The generated
  tool-doc surface is `src/kdive/mcp/app.py` + `src/kdive/mcp/tools/_docmeta.py`.
- **Reaper seam** is `src/kdive/reconciler/provider_reaping.py` (sweeps orphaned
  provider state). **Mock provider** for CI is `src/kdive/providers/fault_inject/`.
- **CLI** is `src/kdive/cli/` with verbs in `cli/commands/{reads,mutations}.py`,
  dispatch in `cli/dispatch.py`, transport in `cli/transport.py`.
- **No** `opentelemetry`, `prometheus`, or service-health HTTP endpoints exist today
  (the only "readiness" in-tree is guest-boot readiness, a different concept).

## File structure

**Telemetry track (new + modified)**

- `src/kdive/observability/__init__.py` (new) — the OTel facade: `LoggerProvider` /
  `TracerProvider` / `MeterProvider` init, the three exporters (stdout-JSON,
  OTLP-default-off, `/metrics`), the redacting log-record/span/metric processors +
  identifier-label allowlist, the bootstrap-ordering stdout floor, sampling. The one
  module that imports `opentelemetry.sdk._logs` (isolated).
- `src/kdive/health/__init__.py` (new) — shared backend-health probe (per-process
  dependency sets, success-cached/failure-immediate), `/livez` loop-heartbeat,
  `/readyz`, `/metrics` handlers, the loopback-bound aux HTTP listener.
- `src/kdive/config/manifest.py` (modify) — register `KDIVE_OTEL_*` + health keys.
- `src/kdive/__main__.py` (modify) — install the stdout floor first; start the aux
  listener per process; wire the loop heartbeat.
- `src/kdive/mcp/middleware.py` (modify) — request span + per-tool RED metrics.
- `src/kdive/jobs/worker.py`, `src/kdive/reconciler/loop.py` (modify) — per-job /
  per-pass spans + metrics, the loop heartbeat bump, dequeue-pause on not-ready.
- Compose/Helm under the M2.1 deployment reference (modify) — liveness/readiness/scrape.

**Doctor track (new + modified)**

- `src/kdive/diagnostics/__init__.py`, `checks.py`, `service.py` (new) — the
  `Check`/`CheckResult` framework (three-state, per-check timeout, `provider`), the
  four checks, the aggregating service + provider fan-out.
- `src/kdive/diagnostics/egress_probe.py` (new) — the ephemeral-probe-guest job
  (reaper-marked, opt-in, single-flight).
- `src/kdive/mcp/tools/ops/diagnostics.py` (new) — the authz-gated diagnostics tool.
- `src/kdive/cli/commands/doctor.py` (new) — the `doctor` verb (render + exit code).
- `src/kdive/reconciler/provider_reaping.py` (modify) — honor the probe's active-run
  heartbeat marker.

---

## Sequencing & dependency graph

```
Telemetry:  1 ──▶ 2 ──┐
              └▶ 3 ──┴▶ 4
Doctor:     5 ──▶ 6
              ├▶ 7
              └──────────▶ 8 ◀── (2,3 for /readyz assertions)
```

- **Issue 1 blocks 2–4.** Issues **5–7** are independent of the telemetry track.
- **Issue 8** depends on everything (it is the exit-criterion proof).
- **Parallelizable waves:** `{1}` → `{2, 3, 5}` → `{4, 6, 7}` → `{8}`. Issues 1 and 5
  are the two track-heads and can start together.
- **Shared rebase zones (serialize the *commit* of these, do not parallel-merge):**
  the generated **config reference** (issues 1 & 4) and the **MCP tool-doc /
  registration surface** `mcp/app.py` + `tools/_docmeta.py` (issue 5). Regenerate and
  commit these last within each issue, per the M2.2 playbook.
- **Label mapping (per the M1.3 gotcha):** the design-doc track names map to repo
  labels — telemetry → `area:core-platform`, doctor/diagnostics → `area:security` +
  `area:ops`. Confirm against `gh label list` when filing.

---

## Issue 1 — OTel signal foundation (telemetry track head)

- [ ] **Scope.** Stand up the `kdive/observability/` facade: one `LoggerProvider` /
  `TracerProvider` / `MeterProvider` per process; bridge existing `logging` call sites
  via `opentelemetry.instrumentation.logging.LoggingHandler` (no call-site churn);
  two log exporters — stdout-JSON (ADR-0014 schema **+ additive `trace_id`/`span_id`**,
  always on) and OTLP (default-off); the redacting **log-record + span + metric**
  processors reusing `SecretRedactionFilter` logic + the identifier-label allowlist;
  bootstrap-ordering invariant (stdout floor installed before provider/config/clients);
  parent-based ratio trace sampling; non-blocking drop-not-block export queues with a
  drop self-metric; register `KDIVE_OTEL_*` keys (endpoint, protocol, on/off, sampling
  ratio) in `config/manifest.py`.
- **Files.** Create `src/kdive/observability/__init__.py` (+ submodules as the facade
  grows); modify `src/kdive/config/manifest.py`, `src/kdive/__main__.py` (stdout floor
  first, provider init), and add the dependencies to `pyproject.toml`
  (`opentelemetry-api`/`-sdk`/`-exporter-otlp-proto-grpc`/`-instrumentation-logging`,
  pinned `==`).
- **ADR decisions to implement.** ADR-0090 §1 (spine + bootstrap ordering), §2 (dual
  export + additive fields), §3 (`bind_context` as attributes), §4 (three-signal
  redaction + identifier-label rule), §6 (non-blocking export), §7 (`_logs` facade
  isolation); ADR-0090 §2 trace sampling.
- **Acceptance.** Existing log tests still pass (additive fields don't break them); a
  record with a registered secret is redacted in the stdout output; OTLP off by
  default; the `_logs` import is confined to this package (assert on the import graph).
- **Tests (unit).** Redaction logic over a log body / span attribute / metric label
  (processors in isolation); stdout schema carries the ADR-0014 fields + `trace_id`;
  OTLP-default-off; import-graph test that only `kdive/observability` touches `_logs`.
- **Depends on.** Nothing (track head). **Blocks 2, 3, 4.**

## Issue 2 — Server telemetry + health

- [ ] **Scope.** Instrument the FastMCP server: a span per MCP request and per-tool
  RED metrics in `mcp/middleware.py`. Add the dedicated aux HTTP listener (distinct
  from the public MCP port) exposing `/livez` (loop heartbeat), `/readyz`, `/metrics`,
  bound loopback/pod-local by default (bind address a config key). Implement the shared
  backend-health probe with the **server dependency set: Postgres + MinIO + OIDC**, the
  success-cached/failure-immediate caching asymmetry, and per-check timeouts.
- **Files.** Create `src/kdive/health/__init__.py`; modify `src/kdive/mcp/middleware.py`,
  `src/kdive/__main__.py` (`_run_server` starts the aux listener), `config/manifest.py`
  (health bind-address key).
- **ADR decisions.** ADR-0090 §5 (`/livez` affirmative, `/readyz` per-process set +
  caching asymmetry, trust boundary, dedicated aux listener on all three).
- **Acceptance.** `/readyz` flips not-ready when a stubbed backend probe fails and
  recovers; a failing probe is reflected immediately while a healthy one is cached;
  `/metrics` is not served on the MCP port; the aux listener binds loopback by default.
- **Tests (unit).** `/readyz` flip + caching asymmetry; bind-address default;
  span/metric-emitted redaction variants (now that spans/metrics exist).
- **Depends on 1.** Shares the backend-health module with issue 3.

## Issue 3 — Worker/reconciler telemetry + aux health listener

- [ ] **Scope.** Add the aux HTTP listener to the worker and reconciler (their first
  HTTP surface); per-job and per-pass spans + metrics (job-duration, queue-depth,
  reconcile-lag); the `/livez` loop heartbeat bumped at **scheduling/poll granularity,
  not per job** (so long builds don't read not-live); `/readyz` via the shared probe
  with the **worker/reconciler dependency set: Postgres + MinIO, no OIDC**; pause
  dequeuing new jobs while not-ready.
- **Files.** Modify `src/kdive/jobs/worker.py`, `src/kdive/reconciler/loop.py`,
  `src/kdive/__main__.py` (`_run_worker`/`_run_reconciler` start the listener + own the
  heartbeat); reuse `src/kdive/health/`.
- **ADR decisions.** ADR-0090 §5 (loop-granularity liveness, per-process readiness,
  dequeue-pause).
- **Acceptance.** `/livez` stays green across a simulated long-running job; worker/
  reconciler `/readyz` does **not** couple to OIDC; not-ready pauses dequeue.
- **Tests (unit).** Long-job-stays-live; readyz omits OIDC; dequeue-pause on not-ready.
- **Depends on 1.** Shares the backend-health module with issue 2.

## Issue 4 — Deployment probe + scrape wiring + config reference

- [ ] **Scope.** Wire compose + Helm (the M2.1 deployment reference) liveness/
  readiness/scrape to the aux endpoints for all three processes; regenerate the
  `KDIVE_*` config reference to include the new `KDIVE_OTEL_*` + health-bind keys.
- **Files.** Modify the compose/Helm references under the M2.1 deployment dir; run the
  config-reference generator (the generated doc — **shared rebase zone**, commit last).
- **ADR decisions.** ADR-0090 consequences (deployment wiring; generated reference
  gains the keys).
- **Acceptance.** Compose/Helm probes target the aux endpoints; the generated config
  reference lists every new key; the structural compose test (docker-plugin-gated, per
  M2.1) passes.
- **Tests (integration, CI).** Reuse the M2.1 compose/Helm structural test; assert the
  probe wiring and the regenerated reference.
- **Depends on 2 + 3.**

## Issue 5 — Diagnostics framework + server/worker-vantage probes + MCP tool (doctor track head)

- [ ] **Scope.** Build `kdive/diagnostics/`: the `Check` abstraction and
  **three-state `CheckResult{status: pass|fail|error, detail, fix, provider}`** with a
  per-check timeout and the explicit **provider target / fan-out**; the three
  read-only checks — `secret_ref` (server vantage, **full coverage**, aggregate +
  platform-ref-only reporting, never per-tenant identifiers), `provider_tls` (worker
  job; host-unreachable→`error`, cert-invalid→`fail`), `gdbstub_acl` (worker job; the
  **ACL/port-range policy** check on `config.gdb_addr`, not a per-domain live port);
  and the authz-gated aggregating diagnostics MCP tool (`platform_operator` via
  `ops/_auth.py`, audited under `(principal, operator-cli)`). State the
  `doctor`-depends-on-core boundary (a worker that can't pick up the job → `error`
  pointing at the health endpoints, not a hang).
- **Files.** Create `src/kdive/diagnostics/{__init__,checks,service}.py`,
  `src/kdive/mcp/tools/ops/diagnostics.py`; modify `src/kdive/mcp/app.py` +
  `tools/_docmeta.py` (tool registration/doc — **shared rebase zone**, commit last).
- **ADR decisions.** ADR-0091 §1 (server-side + core-up boundary), §2 (three-state +
  per-check timeout + provider field/target, the three read checks incl. gdbstub policy
  + secret_ref coverage/non-disclosure), §4 (auth boundary).
- **Acceptance.** The tool is reachable only behind the platform-role gate (denied +
  audited otherwise); a down dependency yields `error` (with a blocked-reason detail),
  not a contract `fail`; `secret_ref` reports aggregate counts, never per-tenant refs.
- **Tests (unit + mock-provider CI).** Three-state mapping incl. `check-cannot-run →
  error`; `provider_tls`/`gdbstub_acl` against seeded-broken/healthy fixtures asserting
  status **and** exact `fix`; authz denial is audited.
- **Depends on.** M2.2 CLI + M2 #202 exec seam (both merged). **Blocks 6, 7.**

## Issue 6 — Ephemeral-probe-guest egress check (heaviest)

- [ ] **Scope.** Implement `guest_egress`: provision a tiny short-lived guest on the
  target provider **under a reaper-visible marker carrying an active-run heartbeat +
  hard TTL**, exec a presigned `HEAD`/`PUT` against object-store from inside it, tear
  down (best-effort; reaper backstop). Make it **opt-in (`doctor --with-egress`)** and
  **single-flight per provider** (a second caller attaches to the in-flight result).
  Teach `reconciler/provider_reaping` to reap a leaked probe but **never reap one whose
  owning run is still live** (honor the heartbeat).
- **Files.** Create `src/kdive/diagnostics/egress_probe.py`; modify
  `src/kdive/diagnostics/service.py` (opt-in/single-flight), `src/kdive/mcp/tools/ops/diagnostics.py`
  (`--with-egress` plumbing + distinct audit of the provisioning action),
  `src/kdive/reconciler/provider_reaping.py` (heartbeat-honoring sweep).
- **ADR decisions.** ADR-0091 §3 (ephemeral probe guest, reaper-owned cleanup, opt-in,
  single-flight), §4 (mutating-check callout / distinct audit).
- **Probe-image prerequisite (named).** On local-libvirt the probe reuses the existing
  fixture image (so the CI/mock-provider tier is self-contained); the **remote** provider
  has no managed probe image until M2.4, so the remote live proof needs an
  **operator-staged** image — an explicit gate precondition, not assumed.
- **Acceptance.** A blocked guest→object-store path yields `fail` with the
  open-the-FORWARD fix; a leaked probe is reaped; an in-use probe is never reaped;
  concurrent invocations spin exactly one guest.
- **Tests (mock-provider CI + live).** Mock provider: leaked-probe reaping, egress
  `fail`/`pass` against seeded-blocked/healthy. Live operator-run: real-guest egress on
  the remote stack (issue 8 records it).
- **Depends on 5.**

## Issue 7 — `kdivectl doctor` verb

- [ ] **Scope.** Add the `doctor` verb to the CLI: call the diagnostics tool over the
  authenticated transport, render the verdict as a table (per check: status, detail,
  fix, **provider**), and set the exit code — **nonzero on any `fail`**, while an
  `error` is reported distinctly and does **not** count as a passed contract. Support
  the explicit provider target and the `--with-egress` opt-in flag.
- **Files.** Create `src/kdive/cli/commands/doctor.py`; modify `src/kdive/cli/dispatch.py`
  (verb wiring), `src/kdive/cli/commands/__init__.py`.
- **ADR decisions.** ADR-0091 §5 (exit-code semantics, two gate runs, provider in the
  verdict).
- **Acceptance.** `doctor` exits nonzero on a `fail`, zero on all-`pass`, and treats
  `error` as not-a-pass (gate-safe); the default run executes the three read checks,
  `--with-egress` adds the probe.
- **Tests (unit).** Verdict rendering incl. `provider` + three-state; exit-code mapping
  (`fail`→nonzero, `error`→nonzero-but-distinct, all-`pass`→zero).
- **Depends on 5.**

## Issue 8 — Fault-seeding exit-criterion proof + operator runbook

- [ ] **Scope.** The milestone exit proof (mirrors the M2.2 boundary-test pattern):
  seed each of the four faults (broken TLS chain, closed gdb ACL, missing secret ref,
  blocked guest→object-store egress) and assert `doctor` names the **exact fix**;
  assert a `check-cannot-run` case maps to `error`, not `fail`; assert `/readyz` goes
  not-ready with a backend down on all three processes; write the operator runbook (the
  band-gate run opts into `--with-egress` and stages the remote probe image).
- **Files.** Create the exit-criterion test (under `tests/integration/`, mock-provider
  for the seedable faults) + the operator runbook under `docs/runbooks/` (house
  location used by prior milestones).
- **ADR decisions.** Spec exit criteria + band-gate evidence (independently-checkable
  per-probe results).
- **Acceptance.** All four seeded faults flagged with the exact fix; the `error`-vs-
  `fail` distinction proven; `/readyz`-down proven on three processes; runbook names the
  remote probe-image precondition.
- **Tests (mock-provider CI + live operator-run).** The seeded-fault assertions run in
  CI via `fault_inject`; the real-guest egress proof is recorded operator-run on the
  remote stack as band-gate evidence.
- **Depends on all (1–7).**

---

## Self-review (plan ↔ spec coverage)

- Spec Telemetry decisions → issues 1–4; every ADR-0090 hardened point is assigned
  (bootstrap ordering §1→1, additive fields §2→1, three-signal redaction §4→1, trust
  boundary/liveness/readiness §5→2+3, non-blocking §6→1, facade §7→1, sampling→1).
- Spec Diagnostics decisions → issues 5–7; every ADR-0091 hardened point is assigned
  (three-state §2→5, reaper/opt-in/single-flight §3→6, auth §4→5, exit semantics §5→7).
- Spec Testing tiers → each issue carries its tier; the live tier + the four-fault
  proof land on issue 8.
- Prerequisites (remote probe image) and coordination (rebase zones, label mapping)
  are stated in Sequencing and issue 6.
- No placeholders; file paths are the verified tree paths from Grounding facts.
