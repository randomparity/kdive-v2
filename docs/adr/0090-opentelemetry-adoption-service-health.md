# ADR 0090 — OpenTelemetry adoption, log-signal migration & service health (M2.3)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Amends (does not discard):** [ADR-0014](0014-structured-logging.md) (the JSON-on-stdout
  structured-logging contract, its field schema, and `bind_context` — all preserved; what
  changes is the transport, now the OTel log pipeline, and correlation — native trace context
  for in-span records, with `request_id`/additive `trace_id` fields on the stdout path).
- **Builds on:** [ADR-0087](0087-config-registry.md) (the `KDIVE_*` registry the
  `KDIVE_OTEL_*` keys extend), [ADR-0088](0088-deployment-packaging.md) (the M2.1 image /
  compose / Helm reference whose probes target the new health endpoints).
- **Spec:** [`../superpowers/specs/2026-06-10-m23-observability-doctor-design.md`](../superpowers/specs/2026-06-10-m23-observability-doctor-design.md)
- **Milestone:** M2.3

## Context

kdive emits structured JSON logs (ADR-0014) but no metrics and no traces, and its three
processes (`server`, `worker`, `reconciler`) expose no service-health surface. Driving M2 on
real hardware made both gaps costly: a wedged worker or a reconcile loop falling behind is
invisible, and the M2.1 deployment reference has nothing to probe for "able to do work" versus
"process up." ADR-0014 deliberately took **no third-party dependency**; metrics and traces
cannot be added without reversing that for at least the telemetry signals.

Three codebase facts constrain the choice:

- Logging is centralized in `src/kdive/log.py` with a fixed field schema and a
  `SecretRedactionFilter` on the emit path; many call sites use the plain `logging` API.
- The worker (`jobs/worker.py`) and reconciler (`reconciler/loop.py`) are asyncio loop
  processes with **no HTTP server**; only the FastMCP `server` is HTTP today.
- The platform's flagship M2.3 feature, `doctor`, exists because *reachability silently
  breaks*. A telemetry pipeline that goes dark on unreachability would share that failure mode.

## Decision

1. **Adopt OpenTelemetry as the single signal spine — logs, metrics, and traces.** Metrics
   and traces are net-new. Logs **migrate onto the OTel log pipeline**: one `LoggerProvider`
   per process, with existing `logging.getLogger(...)` call sites bridged in unchanged via
   `opentelemetry.instrumentation.logging.LoggingHandler` (no call-site churn). Trace context
   (`trace_id`/`span_id`) attaches to log records **natively** under an active span; no
   hand-rolled context injection. **Bootstrap ordering is an invariant:** a stdlib stdout
   handler (the ADR-0014 JSON formatter, no OTel dependency) is installed as the *first* step of
   process startup, before the `LoggerProvider`, config-registry validation (ADR-0087), or any
   backend client is constructed — so records emitted during early startup, including the
   config-validation failures that are the most common first-run fault, are never lost to an
   unconfigured root logger. The OTel handler is added once the provider is built; the stdout
   floor is continuous across the whole startup.

2. **Dual log export; stdout is the floor, OTLP is opt-in.** The log pipeline carries two
   exporters: a **stdout exporter preserving ADR-0014's JSON field schema, plus two additive
   fields, `trace_id` and `span_id`** (always on — kubelet scrapes it under k8s, journald
   captures it under systemd, it is on the terminal in a bare venv), and an **OTLP exporter for
   cross-host push, default-off**, enabled by `KDIVE_OTEL_*`. The two trace fields are
   *additive*: every ADR-0014 field keeps its name and meaning, so existing log consumers and
   the log tests are unbroken, and an operator reading `kubectl logs`/`journalctl` (the stdout
   path — which is exactly the path in use when the collector is down) can still correlate a
   record to its trace. stdout-only is a complete, correct deployment. This keeps the
   venv/systemd consumption model (how M2 was run) first-class and ensures the observability
   pipeline does **not** share the unreachability failure mode `doctor` diagnoses. Metrics and
   traces export over OTLP under the same switch; a `/metrics` scrape endpoint also exposes
   metrics so a pull-based collector works without OTLP. **Traces are sampled by contract, not
   100%:** the default is parent-based ratio sampling with the ratio a `KDIVE_OTEL_*` key, and
   error/slow spans always sampled. Unbounded head sampling is the standard production
   cost/volume footgun and would itself drive the decision-6 export queues to their drop
   threshold — shedding the traces you wanted because of the ones you didn't — so the sampling
   ratio is a defined config lever from day one, not a post-incident discovery.

3. **`bind_context` survives as domain context.** `request_id`, `job_id`, `principal`,
   `object_id`, `transition` are carried as OTel log attributes — orthogonal to trace context
   and still the primary key for correlating a request/job across processes (and the
   correlation key on the stdout path that does not require an active span).

4. **Redaction runs at the OTel SDK boundary, across all three signals — logs, traces, and
   metrics.** Adopting OTLP adds two *new* secret-egress paths besides logs: span attributes
   and span events routinely carry secret-bearing data (a connection URL with an embedded
   token, an exception message, a request parameter), and metric labels can too. The redaction
   invariant is therefore **not** a `logging` filter (which only covers the log signal) — it is
   enforced on the export boundary of every signal: a redacting **log-record processor**, a
   redacting **span processor**, and a redacting **metric view/attribute filter**, each running
   before its exporter. The existing `SecretRedactionFilter` redaction logic is reused, but its
   placement moves from the stdlib log path to these three SDK hooks. A registered secret placed
   in a log body, a span attribute, **and** a metric label is redacted in every exporter's
   output — a single dedicated test asserts all three, because the failure mode is "logs are
   clean so we assumed traces were too." Shipping an unredacted secret to an external collector
   is worse than a noisy local log; that holds for whichever signal carries it.

   Redaction scrubs secret *values*; **identifiers are a separate disclosure surface** governed
   by a companion rule. Metric labels and span attributes must **not** carry raw tenant /
   `principal` / project / secret-ref identifiers as free-cardinality labels — both because
   high-cardinality labels are a metrics-cost footgun and because, per ADR-0089, who-and-what
   exists is itself reconnaissance data. A label allowlist (a fixed, reviewed key set) is the
   default; identifiers travel as log attributes (already access-controlled by the log path),
   not as metric/trace labels.

5. **Service health on all three processes via a minimal aux HTTP listener.** **All three
   processes** — including the server — expose `/livez`, `/readyz`, and `/metrics` on a
   **dedicated auxiliary HTTP listener bound to a side port, distinct from the server's public
   MCP listener** (the worker and reconciler have no HTTP today and gain only this aux listener;
   the server's MCP app stays clean of health/metrics routes). **These endpoints are an
   operational surface, not a public one:** the aux listener binds **loopback / pod-local by
   default** (or is otherwise network-policy-scoped to the probe/scrape source). They carry no
   authentication of their own, so the network boundary *is* their access control — an
   unauthenticated `/readyz` that triggers backend calls must not be reachable by arbitrary
   clients. The bind address/port is a **defined, validated config key** (decision: the
   `KDIVE_*` registry, ADR-0087) with the loopback/pod-local default, so the boundary is
   enforced by the config contract, not by implementation memory — widening it is an explicit,
   reviewed act. This pairs with the decision-4 label rule: even reached, `/metrics` exposes no
   tenant/principal identifiers.
   - **`/livez` is an affirmative liveness signal, not liveness-by-timeout — and it tracks the
     loop, not the work unit.** The aux listener bumps a monotonic last-tick timestamp at the
     loop's **scheduling/poll granularity** (the loop woke, is dequeuing, has not deadlocked),
     **not** at job completion; `/livez` fails when that timestamp is staler than a configured
     bound. Tracking the poll cycle rather than the job is essential: kdive jobs legitimately
     run for minutes (kernel build, install/boot-readiness waits, vmcore capture), so a
     per-job heartbeat would go stale during healthy long-running work and let K8s kill a
     worker mid-build. A genuinely *stuck* job is caught by job-duration metrics and per-job
     timeouts (decision 6 / the worker's own timeouts), not by liveness. This holds
     **regardless of how the aux listener is threaded** — if the listener runs off a separate
     thread/loop (so it can still answer while the work loop is wedged), a stale heartbeat
     still reports not-live: a wedged-but-alive worker (Context) reads as unhealthy, not
     falsely green, while a busy-but-progressing worker reads as live.
   - **`/readyz` gates on each process's *own* dependency set, not a one-size probe.** A
     **shared probe library** supplies the individual checks (Postgres `SELECT 1`, MinIO bucket
     `HEAD`, OIDC discovery reachable); each process composes only the checks for backends it
     actually uses — **server:** Postgres + MinIO + OIDC; **worker / reconciler:** Postgres +
     MinIO (they pull jobs from the DB and never verify tokens, so their readiness must **not**
     couple to the IdP). `/readyz` is not-ready when one of *that process's* dependencies is
     down. **The effect of not-ready differs by process and is intended:** for the **server**
     (behind a Service) it withdraws the replica from traffic routing; the **worker and
     reconciler** front no Service, so their `/readyz` gates *rollout progression* and feeds
     dashboards, and a not-ready worker **pauses dequeuing new jobs** while a needed backend is
     down rather than failing them. To avoid probe-induced load and readiness flapping under
     K8s probe cadence, check
     results are cached with a **deliberate asymmetry: a healthy result is cached for a short
     TTL** (smoothing load and brief blips), **a failing result is reflected immediately and not
     cached** — so caching never opens a "ready-while-down" window where K8s routes traffic to a
     process whose backend has actually failed. Each check is also bounded by a per-check
     timeout (a hung backend reads as down, not as a stalled probe).

   A heartbeat-file/exec-probe and a push-only/process-alive model were both rejected: the aux
   listener gives a uniform probe model across processes and a real "not-ready when a backend is
   down" signal that a process-alive check cannot.

6. **Export degradation cannot degrade the live service.** All three OTLP exporters use
   **bounded, non-blocking, drop-not-block** batch queues: when the collector is slow, full, or
   down, records are dropped, never blocked-on — an emitting request or job thread is never
   stalled by telemetry. Drops are counted in a self-metric (visible on `/metrics`) so silent
   loss is observable. Combined with decision 2, a dead collector costs at most dropped OTLP
   telemetry; stdout logs and local `/metrics` are unaffected.

7. **Isolate the pre-stable logs SDK behind a facade.** The Python OTel **logs signal is still
   under the `_logs` underscore namespace** (`opentelemetry.sdk._logs`) — the data model is
   stable but the SDK API is not yet promoted, unlike metrics/traces which are GA. All OTel
   wiring lives in `kdive/observability/`, so an upstream API shift is a single-file change.
   Combined with decision 2, the stdout floor does not depend on the `_logs` API at all, so
   logging is never hostage to a pre-stable surface.

## Consequences

- **New pinned dependencies:** `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-grpc` (and/or `-http`),
  `opentelemetry-instrumentation-logging`. ADR-0014's no-dependency stance is reversed for
  telemetry only; the stdout JSON path remains stdlib.
- The exact stdout-exporter mechanism that reproduces ADR-0014's JSON schema plus the additive
  `trace_id`/`span_id` fields (a custom console log exporter vs. keeping the stdlib formatter on
  the console handler and bridging only the OTLP side) is an implementation choice settled in
  the foundation issue; both preserve the existing field contract and the existing log tests,
  which assert presence/values of the ADR-0014 fields and are unaffected by added fields.
- The worker/reconciler gain a small HTTP surface they did not have, and the server gains a
  second listener distinct from its MCP port; in every case it is health/metrics only, bound
  loopback/pod-local by default on a side port, not an API.
- The M2.1 compose/Helm reference wires liveness/readiness/scrape to these endpoints; the
  generated config reference gains the `KDIVE_OTEL_*` keys.
- The provider seam and the agent-facing MCP tool surface are unchanged.
