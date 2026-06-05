# ADR 0035 — Walking-skeleton end-to-end integration-test harness (M0)

- **Status:** Proposed — the **gated full-path tier** (§1, gated portion) is superseded, and
  §3 is **amended for the live tier**, by [ADR-0042](0042-live-stack-e2e-mcp-http.md). The
  **non-gated** criterion tier (§1 non-gated portion: #3/#4/#6), its in-process
  `RequestContext` construction (§3), the teardown-via-`Discovery.list_owned` assertion
  (§2, #5), and the pinned fixtures + preflight (§4) remain in force.
- **Date:** 2026-06-04
- **Issue:** #26 (M0: End-to-end walking-skeleton integration test)
- **Depends on:** every M0 plane (#3–#25) — this issue exercises their handlers, not
  new behaviour:
  [ADR-0023](0023-discovery-allocation-admission.md) (`allocations.*`),
  [ADR-0026](0026-investigation-run-lifecycle.md) (`investigations.*`, `runs.*`),
  [ADR-0025](0025-provisioning-plane-libvirt.md) (`systems.*` + provision/teardown handlers),
  [ADR-0029](0029-build-plane-local-make.md)/[ADR-0030](0030-install-boot-plane.md)
  (`runs.build/install/boot` handlers + the `run_steps` ledger),
  [ADR-0032](0032-connect-plane-gdbstub-debugsession.md)/[ADR-0034](0034-debug-plane-gdbmi-tier.md)
  (`debug.*`),
  [ADR-0028](0028-control-plane-power-force-crash.md) (`control.force_crash` + the
  three-check gate),
  [ADR-0031](0031-retrieve-plane-vmcore-postmortem.md) (`vmcore.fetch`, `artifacts.get`),
  [ADR-0006](0006-oidc-rbac-attribution.md)/[ADR-0020](0020-rbac-audit-gate-implementation.md)
  (audit + gate),
  [ADR-0027](0027-safety-modules-secret-backend-impl.md) (the `Redactor`),
  [ADR-0015](0015-sql-migration-runner.md) (the disposable-Postgres fixtures the
  non-gated tests reuse).
- **Spec:** [`../specs/m0-walking-skeleton.md`](../specs/m0-walking-skeleton.md) §"Exit criteria"

## Context

The M0 spec pins six falsifiable exit criteria for the walking-skeleton path
(`allocations.request → … → allocations.release`). Each criterion needs an assertion.
The path's happy spine — provision a real libvirt domain, `make` a kernel, boot it,
attach gdb over the RSP stub, crash via `sysrq-c`, capture a kdump vmcore — only runs
against an operator-provided KVM/libvirt host with a kdump-enabled guest image, which
GitHub-hosted runners do not provide. So the test cannot be a single end-to-end run if
CI is to exercise any of it.

This ADR settles the harness decisions the spec leaves open: how the six criteria split
across a gated and a non-gated tier, how teardown is verified without a real hypervisor,
where the mock OIDC issuer lives, and how the live fixtures stay reproducible. The plane
behaviour itself is already decided in the per-plane ADRs; nothing here changes a handler.

## Decision

### 1. Two tiers: a `live_vm`-gated full-path test, plus non-gated handler-level tests for the criteria that don't need a hypervisor

*Superseded by [ADR-0042](0042-live-stack-e2e-mcp-http.md) — the gated full-path test is
replaced by `tests/integration/test_live_stack.py` (a wire-driven `live_stack` test); the
non-gated tier below remains in force.*

~~`tests/integration/test_walking_skeleton.py` carries both. The full spine
(`@pytest.mark.live_vm`, `# pragma: no cover - live_vm`) reads its fixture locations from
env (`KDIVE_GUEST_IMAGE`, `KDIVE_KERNEL_SRC`) and `pytest.skip`s with an actionable reason
when they are absent — the same idiom every merged plane uses for its real-host smoke test
(e.g. `tests/providers/local_libvirt/test_build.py::test_live_vm_real_make_build_id_matches_readelf`).
It never runs on `pull_request`; PR CI stays green by deselecting `-m "not live_vm"`.~~

Three of the six criteria are decided by **policy over data**, not by the hypervisor, so
they are written as non-gated tests that call handlers directly with injected fakes — the
repo's unit of testing ([ADR-0019](0019-tool-response-envelope.md); handlers, never MCP):

- **Gate refusal (#6).** `control.force_crash_system` denies and audits when any of the
  three checks (capability scope, `admin` role, profile opt-in) is absent. Pure policy
  over `(ctx, allocation, system_profile)`; no domain involved.
- **Idempotent replay (#4).** A second dispatch of a completed `build`/`install`/`boot`
  job reads the `(run_id, step)` ledger and does **not** re-invoke the injected provider
  (asserted on the fake's call count). The ledger is plain SQL; no domain involved.
- **Redaction (#3).** Two independent in-process mechanisms, asserted separately. (a)
  *Transcript-text redaction:* a planted secret in a provider's transcript output is
  `[REDACTED]` in the `postmortem.crash`/`.triage` envelope's `data["transcript"]` (the
  `Redactor` runs before the value is returned). (b) *Artifact sensitivity:* the raw
  vmcore stays `sensitive` and is unreachable through `artifacts.get`/`vmcore.list`, which
  filter to `redacted` rows and return only object-key refs (never artifact content). No
  domain involved.

*Superseded by [ADR-0042](0042-live-stack-e2e-mcp-http.md) — #1/#2/#5 are now asserted by
the live-stack wire driver, not this gated test.*

~~The remaining three (#1 path completes, #2 every transition audited, #5 teardown leaves
no orphan) are asserted inside the gated full-path test, where a real domain exists to
provision and tear down.~~

*Superseded by [ADR-0042](0042-live-stack-e2e-mcp-http.md) §1 — the queue-drive contract is
honoured by the shipping `worker`/`reconciler` processes draining the queue, not an in-test
`Worker.run_once()`.*

~~**Queue-drive contract (gated test).** Five steps are async job kinds (`provision`,
`build`, `install`, `boot`, `capture_vmcore`); each returns a job handle and only its
*committed* result unblocks the next dependent tool — `runs.boot` returns
`install_first` until a succeeded `install` `run_steps` row commits, and
`debug.start_session` requires a succeeded `boot` step. So the gated test drives each
enqueued job to `succeeded` through the **production worker spine** — `Worker.run_once()`
(`jobs/worker.py`) dequeuing under the real lease, dispatching the registered handler —
before issuing the next dependent tool, rather than calling handlers inline. This keeps
criterion #1's "real path" honest (the dequeue/lease/handler path is what ships) and makes
the ordering edges deterministic instead of racing admission against the next call.~~

**Considered & rejected — a non-gated "inline-worker" full path with fakes for every
plane.** We could drive the *entire* spine in CI by injecting a fake `Provisioner`,
`Builder`, `Installer`, `Booter`, `Connector`, gdb-MI engine, and `Retriever`, then
calling each handler in sequence. Rejected as the *primary* end-to-end signal: with every
provider faked, criterion #1 ("succeeds against a real libvirt host, producing a fetchable
redacted vmcore") is no longer what is being proven — the test would assert the wiring of
fakes, not that the model holds on real infrastructure, which is the whole point of M0's
exit. The genuinely hypervisor-independent criteria (#3/#4/#6) are extracted as their own
focused non-gated tests instead, so CI exercises real signal without masquerading a
fully-faked run as the end-to-end proof. The per-plane handler suites already cover the
fake-driven wiring of each step.

### 2. Teardown (#5) is verified through the **Discovery** provider's `list_owned`, not the `Provisioner`

The spec phrases #5 as "the reconciler leaves no orphaned libvirt domain (`list_owned` is
empty of unowned domains)." Two distinct provider seams are in play and must not be
conflated: the `Provisioner` port the `systems.teardown` handler injects exposes only
`provision(system_id, profile)` and `teardown(domain_name)` — it *destroys* a domain but
cannot enumerate the host. Enumeration is the **Discovery** seam: `LocalLibvirtDiscovery.list_owned()
→ list[OwnedInfra]` (each `{system_id, domain_name}` parsed from the kdive libvirt
metadata tag), which is also what the reconciler's leaked-domain repair consumes through
its own `OwnedDomainSource.list_owned()` protocol.

So in the gated test against a real host: after `allocations.release` drives the System
`torn_down` and the teardown job (via `Provisioner.teardown`) destroys the domain, the
test asserts the System row is `torn_down` **and** that the released System's `system_id`
appears in **no** `OwnedInfra` returned by `Discovery.list_owned()` (equivalently, no
kdive-tagged domain survives on the host). The assertion is on the Discovery surface, not
only the DB row, because a `torn_down` row with a surviving tagged domain is exactly the
orphan the criterion forbids.

**Considered & rejected — asserting only the `systems.state = 'torn_down'` DB row.** That
checks the bookkeeping, not the physical cleanup, so it would pass even if `teardown`
silently failed to destroy the domain — the precise leak #5 exists to catch. The
`list_owned` enumeration is the authoritative check.

### 3. ~~The mock OIDC issuer lives only in `docker-compose` for a manual live run~~; handler-level tests construct `RequestContext` directly

Every non-gated test builds a `RequestContext(principal, agent_session, projects, roles)`
in-process — the established pattern across the merged plane suites — so no token minting,
JWKS endpoint, or network is on the unit path. *Amended by
[ADR-0042](0042-live-stack-e2e-mcp-http.md) §3 — the live-stack tier now obtains real tokens
from the issuer and validates them through the server's `JWTVerifier`:* ~~The
`docker-compose.yml` (Postgres + MinIO + a mock OIDC issuer) exists to stand up the *server*
for an operator driving the real path by hand or on a self-hosted runner; it is harness
infrastructure, not a per-test dependency.~~

**Considered & rejected — minting real signed JWTs and verifying them through `JWTVerifier`
in every criterion test.** The auth/JWKS path is already covered by `tests/mcp/test_auth.py`
against an in-process `RSAKeyPair` (no container). Threading a live OIDC issuer through every
exit-criterion test would couple them to a network service for no added coverage and slow the
suite. The mock issuer earns its place only for the full server-level live run.

### 4. Reproducible fixtures are pinned shell scripts with a fail-fast preflight

`scripts/live-vm/fetch-kernel-tree.sh` and `scripts/live-vm/build-guest-image.sh` produce
the kernel source tree and kdump-enabled guest image the gated path needs. Both pin their
inputs (an explicit kernel ref; a base image by digest) so a re-run yields the same fixture,
start with `set -euo pipefail`, and pass `shellcheck` + `shfmt` (the pre-commit shell hooks).
A `live_vm` **preflight** at the top of the gated test checks the fixture paths exist and
`pytest.skip`s with the exact script to run when they do not — a missing fixture is a clear,
actionable skip, never a confusing mid-path failure.

**Considered & rejected — downloading fixtures on the fly inside the test.** That makes the
test depend on network availability and upstream artifact stability at run time, and hides
multi-minute downloads inside a "test." Separating fixture *production* (scripts, run once by
the operator) from fixture *consumption* (the test, which only checks presence) keeps the
gated test fast to start and its skip reason honest.

## Consequences

- PR CI runs `pytest -m "not live_vm"`; the three hypervisor-independent criteria
  (#3 redaction, #4 idempotency, #6 gate refusal) execute on every PR against the
  disposable Postgres ([ADR-0015](0015-sql-migration-runner.md)). The full-path test
  (#1, #2, #5) is deselected and reported as skipped — expected and correct.
- The gated test is the M0 exit gate: an operator runs the two fixture scripts, points the
  env vars at their output, and runs `just test-live` (or the `workflow_dispatch` `live_vm`
  job) on a KVM host. Until then the end-to-end proof is "wired and skipped," matching every
  other plane's real-host smoke test.
- No handler changes. If a non-gated criterion test fails, the defect is in the plane it
  exercises, not in this harness — the test is a thin assertion over an existing handler.
- The mock OIDC issuer and MinIO in `docker-compose` are not on any automated test path in
  this issue; they are operator tooling for the live run. A later milestone that adds a
  server-level CI smoke test would wire them in then, not now (no speculative wiring).
