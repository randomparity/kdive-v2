# ADR 0091 — `doctor` / diagnostics model (M2.3)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0089](0089-operator-cli-mcp-client.md) (`kdivectl`
  as an authenticated MCP client — `doctor` is a curated verb on it), the M1.3 platform-role
  gate (`mcp/tools/ops/`) the diagnostics tool is authz-gated behind, the M2 in-guest
  exec/presigned-URL seam (#202) the egress probe execs through, and
  [ADR-0006](0006-oidc-rbac-attribution.md) (the `(principal, operator-cli)` audit attribution
  every diagnostics call records under).
- **Spec:** [`../superpowers/specs/2026-06-10-m23-observability-doctor-design.md`](../superpowers/specs/2026-06-10-m23-observability-doctor-design.md)
- **Milestone:** M2.3

## Context

The faults that cost the most in M2 were *undiagnosed reachability* failures — a provider TLS
chain, the gdbstub-port ACL, a secret-ref that did not resolve, and a guest→object-store
egress path silently dropped by an unrelated host `FORWARD` policy. Each surfaced only as a
downstream job failure with no pointer to the cause. M2.3 adds a `doctor` preflight that probes
these four contracts and names the **exact fix**.

The checks do not share a vantage point. `kdivectl` runs on an operator laptop; it cannot
observe the guest-bridge→object-store hop or the worker→hypervisor TLS chain from there. A
preflight that probes only from the operator's network would false-green on exactly the M2
fault class.

## Decision

1. **`doctor` is a server-side diagnostics tool surfaced as a `kdivectl` verb, not a
   client-side prober.** `kdivectl doctor` calls an authenticated diagnostics MCP tool; the
   deployment runs each probe **from its correct vantage** and returns one coherent verdict.
   A client-side-only model was rejected (it cannot see the egress or worker-vantage paths);
   a client/server hybrid was rejected as two code paths and a result-merge problem for no
   coverage gain over running everything server-side.

   **`doctor` diagnoses a deployment whose core is up; it does not replace the health
   endpoints.** Because the worker-vantage checks run as jobs, `doctor` needs the server
   reachable and the worker/job queue working just to *run* a check — so the division of labor
   is explicit: ADR-0090's `/livez`/`/readyz` answer "are the core processes and their backends
   up," and `doctor` assumes that and diagnoses the *contract/reachability* faults that an
   otherwise-healthy deployment still hides. If the worker cannot pick up the diagnostic job,
   that surfaces as an `error` result pointing at the health endpoints, **not** a hang — the
   tool that explains breakage does not silently wedge on the breakage it is meant to explain.

2. **A `Check` framework with an explicit vantage, a three-state result, and a mandatory fix.**
   A `Check` is `id`, `vantage`, and `run() -> CheckResult{status, detail, fix, provider}`, where
   **`status` is three-state — `pass` / `fail` / `error`** — *not* a boolean. The distinction is
   load-bearing: `fail` means *the contract is violated* and `fix` is the exact remediation;
   **`error` means the check could not be run to a verdict** (the provider was unreachable, the
   probe guest never booted, the secret backend was down) and `detail` says *what blocked the
   check*, **never** a contract-fix string. Collapsing `error` into `fail` is the worst failure a
   diagnostic can have — it would emit "open the guest subnet → MinIO" when the provider was
   simply down, a confident wrong remediation from the one tool whose entire value is naming the
   *right* fix. `fix` is mandatory **on `fail`**; a `fail` check that cannot name the fix is not
   done. **Every check is bounded by a per-check timeout** (a check that does not answer within
   its bound is `error` with a "did not respond within N" detail), and `doctor` carries an
   overall deadline — so used as a CI/deployment gate (decision 5) it reports a clean `error`
   rather than hanging on a black-holed host. The four checks and their vantages:
   - `secret_ref` — **server** vantage — **every** configured secret ref resolves in the backend
     (full coverage: the motivating M2 fault was a ref that did not resolve, and the ADR does not
     assume it was a platform ref vs. a project one, so coverage spans both). Non-disclosure is
     enforced on the **reporting** surface, not by dropping coverage: the verdict reports
     aggregate pass/fail counts and platform-ref detail only — **never** per-tenant/project ref
     identifiers — so the diagnostic catches every unresolved ref without becoming the
     cross-tenant secret-presence disclosure ADR-0089 guards against.
   - `provider_tls` — **worker job** vantage — the provider connection's TLS chain validates
     against the configured CA (host-unreachable is `error`, cert-invalid is `fail`).
   - `gdbstub_acl` — **worker job** vantage — the host firewall/ACL on `config.gdb_addr` admits
     the **configured gdbstub port range/policy**, probed from the host the real debug session
     connects from. This is deliberately a *policy* check, not a live-port check: the gdbstub
     port is assigned per-domain and read from the domain XML (ADR-0083), so a cold preflight
     with zero running guests has no concrete port — validating the ACL admits the configured
     range needs no live domain and catches the M2 fault (a closed ACL) directly. A
     specific-live-port check would need a running debug target, the same constraint `guest_egress`
     resolves with the probe guest; the range/policy check avoids provisioning for this fault.
     For the providers in scope that host *is* the worker (the remote debug client runs
     worker-side, per ADR-0083), so the worker vantage is the production path, not a proxy for
     it; if a future provider connects the debug client from elsewhere, this check must move to
     that vantage rather than silently validating the wrong hop.
   - `guest_egress` — **ephemeral-guest** vantage — a guest on the provider bridge can reach
     object-store.

   The per-provider checks (`provider_tls`, `gdbstub_acl`, `guest_egress`) are inherently
   provider-scoped, so **`doctor` takes an explicit provider target** — a named registered
   provider, or "all registered" which fans the checks out per provider — and **every
   `CheckResult` carries the `provider` it pertains to**. A green result is therefore never
   ambiguous about *which* provider it covered, and a passing provider A cannot mask a broken
   provider B in the band-gate evidence record (decision 5). `secret_ref` is provider-independent
   (its `provider` is unset). Note the egress probe's cost scales with breadth: "all registered"
   with `--with-egress` provisions one probe guest *per* provider, so it is the heaviest run —
   operators scope to a named provider when they do not need the full sweep.

3. **The egress check provisions an ephemeral probe guest — opt-in, single-flight, and
   reaper-owned.** `doctor` is a preflight and may run with zero workload guests, so
   `guest_egress` provisions a tiny short-lived guest on the target provider, execs a presigned
   `HEAD`/`PUT` against object-store **from inside the guest** (the exact hop the M2 `FORWARD
   DROP` broke), and tears it down. A worker-host proxy was rejected: the worker host may take a
   different path and pass while the guest path is still broken — a false-green on the one fault
   this check exists for. A bring-your-own-allocation model was rejected as not a true cold
   preflight (it cannot run with zero allocations).

   Because this check **provisions real, cost-bearing infrastructure** (unlike the other three,
   which only read), three guards apply:
   - **Cleanup is best-effort with a reaper backstop, not assumed.** Teardown can fail — the
     exec hangs, `doctor` is interrupted, the worker job dies — and a leaked booted guest is a
     real slot/cost cost (acute at M3 cloud). The probe guest is provisioned under an **owned,
     reaper-visible marker carrying an active-run heartbeat and a hard TTL**, so the existing
     `reconciler/provider_reaping` sweep reaps it exactly like any other orphaned provider
     resource. The reaper **must not reap a probe whose owning `doctor` run is still live** (it
     honors the heartbeat); the TTL is a *backstop sized well above the probe's max runtime*,
     not a deadline competing with a slow boot — otherwise the reaper would destroy a guest
     mid-check and turn a healthy egress path into a spurious `error`. The ADR does not claim
     teardown always succeeds; it claims a leaked probe is always reaped, and an in-use probe
     is never reaped.
   - **Opt-in, not on by default.** The heavy egress probe runs only when explicitly requested
     (`doctor --with-egress` / a distinct verb); the three read-only checks are the default
     cheap preflight, so the common `doctor` run provisions nothing.
   - **Single-flight per provider.** Concurrent `doctor --with-egress` invocations do not each
     spin a guest — the egress probe is single-flighted per target provider, so a CI loop or two
     operators cannot exhaust a small provider's capacity by diagnosing it. A second caller while
     a probe is in flight **attaches to the in-flight probe's result** (shared single result),
     bounded by the same overall deadline — it neither spawns a second guest nor blocks past the
     deadline.

   The cost — provisioning a guest is heavyweight and needs a bootable image — is accepted (for
   the opt-in run) because catching this fault class is the milestone's highest-payoff outcome.

4. **Same auth boundary as every tool, with the mutating check called out.** The diagnostics
   tool is authz-gated to `platform_operator` (the M2.2 operator boundary), and every invocation
   is audited under `(principal, operator-cli)` (ADR-0006). `doctor` is an operator preflight,
   not an agent capability: it is not on the agent-facing tool path and runs with no raw DB
   credentials. The three read-only checks are pure reads under that gate. The **`guest_egress`
   check is the exception — it consumes provider resources**, so it is not a silent read behind
   a read-shaped verb: it is opt-in and single-flighted (decision 3), and its provisioning
   action is audited distinctly so an operator (or a stolen token) cannot quietly amplify it into
   resource exhaustion under cover of "just running doctor."

5. **`kdivectl doctor` exits nonzero on any `fail` check** (an `error`/indeterminate check is
   reported distinctly and does not count as a passed contract — a gate must not go green on
   checks that could not run) and renders per-check `status`/`detail`/`fix`, so it is usable in a
   deployment/CI gate, not only interactively. There are two distinct gate runs: the
   **per-deploy CI gate** runs the three cheap read checks (no guest provisioned on every
   pipeline run), while the **operator-run milestone exit / band-gate proof opts into the egress
   probe** (`--with-egress`) — so the headline guest→object-store fault *is* gated, just on the
   heavyweight operator-run path, not on every pipeline. "Opt-in" means "not on every CI run,"
   not "ungated." The verdict carries each probe's individual result — with its `provider` — as
   independently-checkable evidence; `doctor` is built in this same band and cannot be its own
   sole oracle for the band gate.

## Consequences

- A new `kdive/diagnostics/` package (the framework + the four checks) and a new
  `mcp/tools/ops/diagnostics.py` tool; the egress check adds a worker job that provisions and
  reaps a probe guest. No change to the provider seam or the agent-facing tool surface.
- The probe guest needs a minimal bootable image on the target provider; on local-libvirt this
  reuses the existing fixture image, and the M2.4 image-lifecycle work makes the per-provider
  probe image first-class. It is provisioned under a reaper-visible marker with a TTL so
  `reconciler/provider_reaping` reaps a leaked probe — `doctor` adds no new orphan class.
- Each check is tested against a seeded-broken, a seeded-healthy, **and a check-cannot-run**
  fixture, asserting that the three map to `fail` (with the exact `fix` string), `pass`, and
  `error` (with a blocked-reason detail, no fix string) respectively; the milestone exit test
  (spec issue 8) seeds all four faults and asserts `doctor` names each fix — the failure is
  asserted, not assumed, and a down-dependency does not masquerade as a contract violation.
- `doctor` is the consumer that justifies ADR-0089's note that operator-CLI calls arriving
  under the agent `client_id` should be flagged; that flag is a `secret_ref`-adjacent
  configuration check candidate but is not in the four-check exit scope.
