# M2.3 — Observability & doctor — Design

**Status:** proposed · **Date:** 2026-06-10 · **Milestone band:** M2.x productionization
**Owner:** David Christensen
**ADRs:** ADR-0090 (OpenTelemetry adoption, log-signal migration & service health),
ADR-0091 (doctor / diagnostics model)
**Band parent:** `docs/superpowers/specs/2026-06-10-m2x-productionization-band-design.md`

## Context

Driving M2 (remote libvirt) end-to-end on real hardware showed kdive is not yet
operable by anyone but its author. Two day-2 gaps dominated the lost time:

1. **No operational visibility.** The platform emits structured JSON logs
   (ADR-0014) but no metrics and no traces. A wedged worker, a slow tool, or a
   reconcile loop falling behind is invisible until a human reads logs by hand.
   The three processes (`server`, `worker`, `reconciler`) expose no service health
   surface, so a deployment cannot tell "process up" from "process able to do
   work" — the M2.1 compose/Helm reference has nothing to probe.
2. **Silent contract violations.** The faults that cost the most in M2 were
   *undiagnosed reachability* failures: a provider TLS chain, the gdbstub-port
   ACL, a secret-ref that did not resolve, and a guest→object-store egress path
   silently dropped by an unrelated `FORWARD` policy. Each surfaced only as a
   downstream job failure with no pointer to the real cause.

M2.3 closes both gaps. It is provider-agnostic and acts on the **service**, not a
provider — it runs against a venv deployment exactly as against the M2.1 image.

## Decision

Deliver two tracks that share only the `KDIVE_*` config seam (ADR-0087):

- **Telemetry & health** — adopt OpenTelemetry as the signal spine (logs, metrics,
  traces), instrument all three processes, and add service health/readiness
  endpoints the M2.1 deployment can probe.
- **Diagnostics & doctor** — a server-side diagnostics tool that runs three
  read-only contract probes by default (plus an opt-in ephemeral-guest egress
  probe), each from its correct vantage, surfaced as `kdivectl doctor`, which names
  the **exact fix** for each failing check.

### Telemetry & health

**All-in on OpenTelemetry as the signal spine.** Metrics and traces are net-new;
logs migrate onto the OTel log pipeline. One `LoggerProvider` per process; the
existing `logging.getLogger(...)` call sites are bridged in unchanged via
`opentelemetry.instrumentation.logging.LoggingHandler`, so there is no call-site
churn. Trace context (`trace_id` / `span_id`) is attached to log records
**natively** by OTel under an active span — the hand-rolled context injection of
earlier drafts is not needed. **Bootstrap ordering is an invariant:** a stdlib
stdout handler (the ADR-0014 JSON formatter, no OTel dependency) is installed as
the *first* startup step — before the `LoggerProvider`, config-registry validation,
or any backend client — so the early-startup records that matter most (config-
validation failures) are never lost to an unconfigured root logger.

**Dual log export, stdout is the floor.** The log pipeline carries two exporters:

- a **stdout exporter that preserves ADR-0014's JSON field schema, plus two
  additive fields, `trace_id` and `span_id`** — always on. Existing fields keep
  their name/meaning (log tests unbroken); the additive trace fields let an
  operator reading `kubectl logs`/`journalctl` (the stdout path, in use exactly
  when the collector is down) correlate a record to its trace. Under Kubernetes the
  kubelet scrapes it; under systemd journald captures it; in a bare venv it is on
  the terminal. stdout is the local-capture floor for *every* deployment shape, and
  it does **not** depend on the pre-stable `_logs` API.
- an **OTLP exporter** for active cross-host push to a collector —
  **configurable, default-off**. stdout-only is a complete, correct deployment;
  enabling `KDIVE_OTEL_*` adds central aggregation. This keeps the venv/systemd
  consumption model (how M2 was actually run) first-class without forcing a
  collector on anyone.

All OTLP exporters (logs/metrics/traces) use **bounded, non-blocking,
drop-not-block** queues: a slow or down collector drops telemetry, never stalls an
emitting request/job thread, and drops are counted in a self-metric. So a dead
collector costs at most dropped OTLP telemetry; stdout and local `/metrics` are
unaffected.

**`bind_context` survives** as kdive *domain* context (`request_id`, `job_id`,
`principal`, `object_id`, `transition`), carried as OTel log attributes. It is
orthogonal to trace context and still the primary correlation key for a single
request/job across processes (and the correlation key on the stdout path that does
not require an active span).

**Redaction runs at the OTel SDK boundary, across all three signals — logs,
traces, and metrics.** OTLP adds two *new* secret-egress paths besides logs: span
attributes/events and metric labels both carry secret-bearing data. Redaction is
therefore **not** a `logging` filter (log-signal only) — it is a redacting
log-record processor, span processor, and metric attribute filter, each running
before its exporter (the existing `SecretRedactionFilter` logic, repointed at these
three SDK hooks). A registered secret in a log body, a span attribute, **and** a
metric label is redacted in every exporter's output. Separately, **identifiers are
a disclosure surface**: metric labels and span attributes must **not** carry raw
tenant / `principal` / project / secret-ref identifiers (a label allowlist; per
ADR-0089 who-and-what-exists is reconnaissance); identifiers travel as log
attributes, not metric/trace labels.

**Metrics & traces.** Per-process `service.name` resource attribute. Server: a
span per MCP request (hooked into existing `mcp/middleware.py`) and RED metrics
(rate / errors / duration) per tool. Worker: span per job, job-duration and
queue-depth metrics. Reconciler: span per pass, reconcile-lag metric. Metrics
export over OTLP (default-off, same switch as logs); a `/metrics` endpoint also
exposes them for scrape (below) so a Prometheus-style pull works without a
collector. **Traces are sampled by contract** (parent-based ratio, ratio a
`KDIVE_OTEL_*` key, errors/slow always sampled), so volume is bounded rather than
discovered in production.

**Service health endpoints on all three processes.** **All three** processes —
including the server — expose `/livez`, `/readyz`, `/metrics` on a **dedicated
auxiliary HTTP listener on a side port, distinct from the server's public MCP
listener** (worker/reconciler have no HTTP today and gain only this aux listener).
The endpoints are an **operational surface, not a public one**: the aux listener
binds **loopback / pod-local by default** (the bind address is a validated config
key, ADR-0087), so widening it is an explicit reviewed act — they carry no auth of
their own, so the network boundary is their access control, and (per the label
rule) `/metrics` exposes no tenant/principal identifiers even if reached.

- `/livez` — **affirmative liveness that tracks the loop, not the work unit.** The
  loop bumps a monotonic last-tick at *scheduling/poll* granularity (woke,
  dequeuing, not deadlocked), **not** at job completion — kdive jobs run for minutes
  (build, boot-readiness, capture), so a per-job heartbeat would go stale during
  healthy long work and let K8s kill a worker mid-build. A *stuck* job is caught by
  job-duration metrics/timeouts, not liveness. A genuinely wedged loop still reads
  not-live.
- `/readyz` — the process can do work, gated on **its own** dependency set (a
  **shared probe library**, not a one-size probe): **server** = Postgres + MinIO +
  OIDC; **worker / reconciler** = Postgres + MinIO (they never verify tokens, so
  they must **not** couple readiness to the IdP). Caching is asymmetric — a healthy
  result cached for a short TTL (smooths probe load/blips), a **failing result
  reflected immediately and not cached** (no "ready-while-down" window) — and each
  check is bounded by a per-check timeout. Not-ready means: for the server, withdraw
  from traffic; for worker/reconciler (no Service), gate rollout/visibility and
  pause dequeuing new jobs while a needed backend is down.
- `/metrics` — scrape surface for the process's metrics.

Compose and Helm (M2.1 artifacts) wire liveness/readiness probes and the scrape
annotations to these endpoints.

### Diagnostics & doctor

**A server-side diagnostics tool, surfaced as `kdivectl doctor`.** The four checks
live at different vantage points; `kdivectl` on an operator laptop cannot observe
the guest→object-store path or the worker→hypervisor TLS chain directly. So
`doctor` does **not** probe from the operator's network — it calls an
authenticated diagnostics MCP tool, and the deployment runs each probe from the
correct vantage and returns one coherent verdict. **`doctor` diagnoses a deployment
whose core is up; it does not replace the health endpoints:** because worker-vantage
checks run as jobs, it needs the server reachable and the worker/queue working just
to run a check. ADR-0090's `/livez`/`/readyz` answer "are the core processes up";
`doctor` assumes that and diagnoses the contract/reachability faults an otherwise-
healthy deployment still hides. A worker that can't pick up the diagnostic job
surfaces as an `error` pointing at the health endpoints, not a hang.

**Check framework** (`kdive/diagnostics/`, new). A `Check` is `id`, `vantage`, and
`run() -> CheckResult{status, detail, fix, provider}`, where **`status` is
three-state — `pass` / `fail` / `error`**: `fail` means the contract is violated and
`fix` is the exact remediation; **`error` means the check could not be run** (the
provider was unreachable, the probe guest never booted) and `detail` says what
blocked it, **never** a contract-fix string. Collapsing `error` into `fail` would
emit a confident wrong fix from the one tool whose value is naming the *right* fix.
`fix` is mandatory on `fail`. Every check is bounded by a **per-check timeout** (a
timeout is `error`, not a hang) and `doctor` by an overall deadline. The
per-provider checks carry the `provider` they pertain to; `doctor` takes an explicit
**provider target** (a named provider, or "all registered" fanning out per
provider), so a green result is never ambiguous about which provider it covered. The
four checks:

| Check | Vantage | What it probes (3-state) | Example `fix` (on `fail`) |
|-------|---------|----------------|--------------------|
| `provider_tls` | worker (job) | the provider connection's TLS chain validates against the configured CA (host-unreachable → `error`, cert-invalid → `fail`) | "virtproxyd cert not signed by configured CA `<path>`; reissue or set `KDIVE_PROVIDER_CA`" |
| `gdbstub_acl` | worker (job) | the host firewall/ACL on `config.gdb_addr` admits the **configured gdbstub port range** — a policy check (the port is per-domain, ADR-0083, so a cold preflight has no live port) | "gdbstub port range `<r>` on `<host>` blocked; open the host firewall / ACL for it" |
| `secret_ref` | server | **every** configured secret ref resolves; the verdict reports aggregate counts + platform-ref detail only, **never** per-tenant ref identifiers (full coverage, non-disclosure on the reporting surface) | "secret ref does not resolve under `KDIVE_SECRETS_DIR`; create the file-ref or fix the path" |
| `guest_egress` | ephemeral guest | a guest on the provider bridge can reach object-store | "guest bridge → object-store blocked (likely host `FORWARD` DROP); allow the guest subnet → MinIO" |

**The egress check provisions an ephemeral probe guest — opt-in, single-flight,
reaper-owned.** `doctor` is a preflight that may run with zero workload guests, so
`guest_egress` provisions a tiny short-lived guest on the target provider, execs a
presigned `HEAD`/`PUT` against object-store **from inside the guest** (the exact hop
the M2 `FORWARD DROP` broke), and tears it down. A worker-host proxy was rejected
(it may take a different path and false-green). Because it provisions real,
cost-bearing infrastructure, three guards apply:

- **Reaper-owned cleanup, not assumed.** Teardown can fail (exec hangs, `doctor`
  interrupted, worker dies). The guest is provisioned under an owned, reaper-visible
  marker carrying an **active-run heartbeat and a hard TTL**, so
  `reconciler/provider_reaping` reaps a leak — and the reaper must **not** reap a
  probe whose owning run is still live (honors the heartbeat; the TTL is a backstop
  sized above max probe runtime, not a deadline competing with a slow boot).
- **Opt-in.** The heavy probe runs only on `doctor --with-egress`; the three read
  checks are the default cheap preflight.
- **Single-flight per provider.** Concurrent invocations don't each spin a guest —
  a second caller attaches to the in-flight result, bounded by the overall deadline.

**Authentication is the same boundary as every tool, with the mutating check called
out.** The diagnostics tool is authz-gated to `platform_operator` (same as the M2.2
admin surface), audited under `(principal, operator-cli)`. `doctor` is an operator
preflight, not an agent capability — no raw DB credentials, not on the agent-facing
path. The three read checks are pure reads; the **`guest_egress` check is the
exception — it consumes provider resources**, so it is opt-in, single-flighted, and
its provisioning action is audited distinctly so it can't be amplified into resource
exhaustion under cover of "just running doctor."

**`kdivectl doctor` verb** calls the tool, renders the verdict as a table (per
check: status, detail, fix, provider), and **exits nonzero on any `fail`** (an
`error` is reported distinctly and does not count as a passed contract — a gate must
not go green on checks that couldn't run). Two gate runs: the **per-deploy CI gate**
runs the three cheap read checks (no guest provisioned per pipeline run); the
**operator-run milestone exit / band-gate proof opts into the egress probe**
(`--with-egress`) — so the headline egress fault is gated on the heavyweight
operator path, not every pipeline.

## Components & isolation

- `kdive/observability/` (new) — OTel facade: provider init, exporter wiring
  (stdout JSON + OTLP, non-blocking), the `KDIVE_OTEL_*` config binding, the
  **redaction-on-export hooks for all three signals** (log-record/span/metric
  processors) + the identifier-label allowlist. **Isolates the pre-stable `_logs`
  SDK API** behind one module so an upstream API shift is a single-file change.
  *Depends on:* `kdive.config`, `kdive.security.secrets`.
- `kdive/health/` (new) — the shared backend-health probe (per-process dependency
  sets, caching asymmetry) + the loop-granularity `/livez`, `/readyz`, `/metrics`
  handlers and the loopback-bound aux HTTP listener used by all three processes.
  *Depends on:* the DB / object-store / OIDC clients, `kdive/observability`.
- `kdive/diagnostics/` (new) — the `Check` framework (three-state result, per-check
  timeout), the four checks, and the aggregating diagnostics service. *Depends on:*
  providers (TLS / gdbstub-ACL probes), the guest-agent exec seam (M2 #202), the
  `reconciler/provider_reaping` marker the probe guest is owned under, secret
  registry, object-store client.
- `mcp/tools/ops/diagnostics.py` (new) — the authz-gated diagnostics MCP tool.
  *Depends on:* `kdive/diagnostics`, the M1.3 platform-role gate.
- `cli/commands/` — the `doctor` verb (read/verdict rendering, exit code).
  *Depends on:* the diagnostics tool over the authenticated transport.

Each unit answers cleanly: what it does, how it is used, what it depends on. The
telemetry track and the doctor track share no **runtime** code, only the `KDIVE_*`
config seam — but they are not conflict-free in parallel: issue 4 regenerates the
config reference for the `KDIVE_OTEL_*` keys and issue 5 adds an MCP tool plus its
own config, so the **generated config reference and the MCP-tool registration/doc
surfaces are shared rebase zones** (the M2.2 wave hit exactly these — `app.py`,
generated docs, tool-doc tests). Serialize the issues that regenerate those
artifacts (regenerate-and-commit last), per the M2.2 playbook, rather than
parallelizing on the assumption that disjoint runtime modules mean disjoint diffs.

## Decomposition (epic + 8 sub-issues)

**Telemetry track**

1. **OTel signal foundation** — SDK + exporters (stdout JSON preserving the
   ADR-0014 schema **+ additive `trace_id`/`span_id`** + OTLP, default-off,
   non-blocking drop-not-block), `LoggingHandler` bridge with **bootstrap-ordering
   invariant** (stdout floor first), native trace correlation, **redaction at the
   SDK boundary across logs/traces/metrics + the identifier-label rule**, **trace
   sampling** config, the `KDIVE_OTEL_*` config binding, per-process `service.name`.
   The `kdive/observability/` facade. *Blocks 2–4.* (ADR-0090)
2. **Server telemetry + health** — request spans, per-tool RED metrics, the
   dedicated aux listener (`/livez` loop-poll heartbeat, `/readyz`, `/metrics`)
   distinct from the MCP port with the **loopback/pod-local bind** default, the
   shared backend-health probe (server set: PG + MinIO + OIDC) with **caching
   asymmetry**.
3. **Worker/reconciler telemetry + aux health listener** — the aux HTTP listener,
   job/reconcile spans + metrics, `/readyz` via the shared probe (**worker/recon
   set: PG + MinIO, no OIDC**), loop-granularity `/livez`, dequeue-pause on
   not-ready. *Shares the backend-health module with 2.* *Depends on 1.*
4. **Deployment probe + scrape wiring** — compose/Helm liveness/readiness/scrape
   config for all three processes; the generated config reference gains the
   `KDIVE_OTEL_*` keys (incl. OTLP endpoint, sampling ratio, health bind address).
   *Depends on 2 + 3.*

**Doctor track** *(independent of 1–4; depends only on M2.2 CLI + M2 #202 exec
seam, both merged)*

5. **Diagnostics framework + server/worker-vantage probes + the MCP tool** —
   the `Check`/**three-state `CheckResult{status,detail,fix,provider}`** abstraction
   with **per-check timeout** and **provider target/fan-out**, `secret_ref` (server,
   full coverage + non-disclosure reporting), `provider_tls` and `gdbstub_acl`
   (worker jobs; gdbstub is the **ACL/port-range policy** check), and the
   authz-gated aggregating diagnostics tool. (ADR-0091)
6. **Ephemeral-probe-guest egress check** — the `guest_egress` check: provision a
   probe guest **under a reaper marker (active-run heartbeat + TTL)**, exec the
   presigned HEAD/PUT from inside, tear down (best-effort, reaper backstop); make it
   **opt-in (`--with-egress`) and single-flight per provider**. *Depends on 5's
   framework.* **Probe-image prerequisite (named, not assumed):** on local-libvirt
   the probe reuses the existing fixture image (so the CI/mock-provider tier is
   self-contained); the **remote** provider has no managed probe image until M2.4
   (ADR-0091), so the remote live egress proof requires an **operator-staged
   bootable probe image** for the M2.3 band-gate run — this is an explicit operator
   obligation for the gate, not silently assumed to exist. (heaviest issue)
7. **`kdivectl doctor` verb** — calls the tool, renders the verdict (incl.
   `provider` and the three-state status), sets the exit code (**nonzero on `fail`;
   `error` distinct, not a pass**). *Depends on 5.*
8. **Fault-seeding exit-criterion proof + operator runbook** — seed each of the
   four faults, assert `doctor` names the exact fix; assert a **`check-cannot-run`
   case maps to `error`, not `fail`**; assert `/readyz` goes not-ready with a
   backend down; the operator runbook (the band-gate run opts into `--with-egress`).
   Mirrors the M2.2 boundary-test pattern. *Depends on all.*

## Testing & exit criteria

**Testing.** Tests are tiered so each lands on the issue that can actually run it,
and the hardening doesn't get deferred as "needs infra":

- **Unit** (no backend) — redaction logic over a log body, a span attribute, and a
  metric label (the redacting processors in isolation); the three-state
  `pass`/`fail`/`error` mapping incl. a `check-cannot-run → error` (blocked-reason
  detail, no fix); `/readyz` flips not-ready on a stubbed-failing probe and reflects
  a failure immediately while caching a healthy result; worker/reconciler `/readyz`
  omits OIDC; `/livez` stays green across a simulated long job (loop heartbeat, not
  per-job). *(redaction-logic at issue 1; the span/metric-emitted variants land at
  issues 2/3 once spans/metrics exist; health at issues 2/3.)*
- **Mock-provider integration (CI)** — using the M1.5 fault-injection mock provider:
  a **leaked probe guest is reaped** by `provider_reaping`; the egress check returns
  `fail` against a seeded-blocked egress and `pass` against a healthy one;
  `provider_tls`/`gdbstub_acl` against seeded-broken and seeded-healthy fixtures,
  asserting status **and** exact `fix` (behavior, not implementation). *(issues 5/6.)*
- **Live operator-run** — the real-guest egress probe against the live remote stack
  (it provisions a real guest), recorded as band-gate evidence. *(issue 8.)*

**Per-milestone exit criteria (band-aligned).**

- `doctor` flags each of the four seeded faults — broken TLS chain, closed gdb
  ACL, missing secret ref, blocked guest→object-store egress — **with the exact
  fix** (issue 8 asserts this, it is not assumed), and a down dependency reads as
  `error`, not a contract `fail`.
- `/readyz` reports not-ready when a backend is down, on all three processes (each
  on its own dependency set).
- The three processes emit metrics and traces over OTLP when `KDIVE_OTEL_*` is set,
  and JSON logs to stdout always; **no secret appears in any of the three signals**
  (logs, traces, metrics).

These feed the band gate (M3-entry signal): an operator-not-the-author runs
`doctor` on a fresh two-host setup and the record carries each probe's individual
result as independently-checkable evidence — `doctor` is built in this same band,
so it cannot be its own sole oracle. The two-host (remote) egress proof requires an
operator-staged probe image (issue 6); until M2.4 makes the remote probe image
first-class, staging it is a named precondition of this gate run, not an assumed
capability.

## Consequences

- **ADR-0014 is amended, not discarded.** The JSON-on-stdout contract, the field
  schema, and `bind_context` all survive; the transport becomes the OTel log
  pipeline and trace correlation becomes native. ADR-0090 records the amendment.
- **New dependencies:** `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-grpc` (and/or `-http`),
  `opentelemetry-instrumentation-logging`. Pinned exact versions; the `_logs`
  SDK API is pre-stable, so the `kdive/observability/` facade isolates it and the
  stdout floor never depends on it.
- The provider seam and the agent-facing MCP tool surface are unchanged.
  Diagnostics is an operator surface alongside the M2.2 admin CLI, on the same
  service layer and authz boundary.
- No renumbering of M3/M4/M5; M2.3 sits in the M2.x band per the parent spec.
