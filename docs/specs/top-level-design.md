# KDIVE Production Architecture Design

## Purpose

Re-design `kdive` from a single-user, local, stdio proof-of-concept into a
production, multi-user service that gives agentic coding environments (Claude
Code, Codex) a complete Linux kernel development and debug lifecycle across
heterogeneous resources: local VMs, remote libvirt hosts, remote bare metal
(PXE/SoL/IPMI/Redfish), PowerVM LPARs on ppc64le, and cloud instances.

This is a **greenfield rewrite**. The existing ~33k-LOC PoC is a reference and a
source of portable modules (redaction, path safety, gdb-MI, drgn introspect,
crash postmortem, run-readiness preflight), but the architecture starts clean.
Implementation language remains **Python**, chosen for native access to the
kernel-tooling ecosystem (drgn, libvirt bindings, crash, the MCP SDK).

## What changes from the PoC

| Concern | PoC | Production |
|---|---|---|
| Tenancy | single-user, local | multi-user hosted service |
| Transport | stdio | MCP over streamable HTTP |
| Central abstraction | run-centric (a run bundles build+boot+debug) | six durable objects with independent lifecycles |
| State | per-run JSON + flock | Postgres (system-of-record) + S3-compatible object store |
| Identity | implicit local user | OIDC/SSO + RBAC, with on-behalf-of agent attribution |
| Accounting | none | metering ledger + enforced budgets/quotas (admission control) |
| Long-running ops | inline | durable job queue + worker tier |
| Resource scope | local x86_64 libvirt only | capability-dispatched providers across many resource kinds |

## Core decisions

These were decided during brainstorming and are load-bearing for everything
below. Each should become an [ADR](../adr/) before implementation.

1. **Greenfield rewrite**, Python.
2. **Multi-user service**; MCP over streamable HTTP.
3. **Six durable objects** (Resource / Allocation / System / Investigation / Run /
   DebugSession), replacing the run-centric model.
4. **First slice targets local libvirt/QEMU** — proven infra, on the new
   architecture, before remote/cloud/bare-metal.
5. **Postgres + object store** for state; Postgres advisory locks replace flock.
6. **OIDC/SSO + RBAC** with `(principal, agent_session)` attribution.
7. **Metering + budgets/quotas** with an admission-control gate on allocation.
8. **Async worker tier + durable job queue**; hard per-tenant sandboxing
   designed-for but deferred.
9. **Capability-based provider dispatch** across narrow per-plane interfaces.

## System topology

```
                  agent (Claude Code / Codex)          human (CLI / future UI)
                            │ MCP (streamable HTTP)              │ REST/gRPC
                            ▼                                    ▼
        ┌───────────────────────────────────────────────────────────────┐
        │                    API / Orchestration Core                    │
        │  • MCP tool surface  • authz (OIDC/RBAC, on-behalf-of)         │
        │  • lifecycle state machines  • admission control (quota/budget)│
        │  • job dispatch  • response shaping (snippets+refs, not dumps) │
        └───────────────┬───────────────────────────┬───────────────────┘
                        │ enqueue jobs              │ read/write state
                        ▼                           ▼
        ┌──────────────────────────┐    ┌──────────────────────────────┐
        │   Durable job queue       │    │  Postgres (system-of-record) │
        │  (provision/build/install │    │  resources, allocations,     │
        │   /debug-op/control jobs) │    │  systems, investigations,    │
        └───────────┬──────────────┘    │  runs, reservations,         │
                    ▼                    │  accounting ledger, audit    │
        ┌──────────────────────────┐    └──────────────────────────────┘
        │   Worker tier (pools)     │    ┌──────────────────────────────┐
        │  run provider operations  │───▶│  Object store (S3-compatible)│
        │  scoped per resource class│    │  vmcores, build outputs,     │
        └───────────┬──────────────┘    │  console/gdb transcripts     │
                    ▼                    └──────────────────────────────┘
   providers: local-libvirt │ fault-inject │ remote-libvirt │ cloud │ baremetal-bmc │ powervm …
```

- **MCP over streamable HTTP** — the service is remote and multi-user; agents
  authenticate with scoped, on-behalf-of tokens.
- **Thin, fast core** — owns state machines, authz, admission control; dispatches
  work and never blocks on a long provision.
- **Worker tier** — pulls jobs from a durable queue; long-running ops are jobs
  with pollable status. Pools are scoped per resource class so a flaky BMC pool
  cannot starve local builds. Hard per-tenant sandboxing is deferred.
- **Postgres = system-of-record** for structured state and accounting/audit
  ledgers; **object store** for bulk artifacts, referenced by row.

## Domain model

Six durable objects. Within the Resource → Allocation → System → Run chain, lower
layers outlive higher ones; **Investigation is a cross-cutting grouping** whose
lifetime is independent of any single Allocation (see below). Each is a Postgres
row with an explicit state machine.

```
(principal / project) ──< Investigation ──┐
                                          ├──< Run ──< DebugSession
   Resource ──< Allocation ──< System ────┘
```

A Run is the join point: it belongs to exactly one System (which fixes its
Allocation) and exactly one Investigation (which may group Runs across many
Allocations).

### Resource

A bookable thing, registered by a provider; long-lived, possibly shared.

- Fields: `id`, `provider`, `kind` (local-libvirt / remote-libvirt / cloud /
  baremetal-bmc / powervm), `capabilities` (arch, CPU model+count, memory, disk,
  PCIe devices, console/control transports: SoL/IPMI/Redfish/HMC/gdbstub),
  `pool`, `cost_class`, `status` (available / degraded / offline / draining).
- Resources are discovered or registered, not created by a run. State is mostly
  health/availability.

### Allocation

A user's claim on a Resource for a window. Authz, admission control, and
accounting live here.

- States: `requested → granted → active → releasing → released`, plus `denied`,
  `expired`, `failed`.
- `requested → granted` passes through **admission control**: capability match,
  RBAC, quota/budget check, **and a capacity check against host headroom**.
  Local-libvirt is "always-yes" only for *chargeback/reservation* — it is still
  capacity-admitted (a concurrent-System cap or resource accounting) so M0/M1 fail
  closed instead of thrashing the single host. Cloud/lab adds a real
  reservation/lease with a chargeback estimate.
- Carries `lease_expiry`, `(principal, agent_session)`; emits accounting events
  on every transition.

### System

A provisioned, bootable instance produced by applying a provisioning profile to
an Allocation.

- States: `defined → provisioning → ready → reprovisioning → failed → torn_down`.
- Identity = (allocation, provisioning profile, resulting OS/target fingerprint).
- One Allocation can host sequential Systems (reprovision in place). A System
  never outlives its Allocation.
- **Installing a new kernel and rebooting does not make a new System** — only an
  OS reprovision does.

### Investigation

A bug-chasing campaign that groups the Runs iterating toward a fix.

- States: `open → active → closed`, plus `abandoned`. Closing is explicit (the
  agent resolves the bug or gives up); an Investigation idle past a retention
  window is reconciled to `abandoned`. Closure never cascades to its Runs — they
  stay queryable for narrative and cost audit.
- Scoped to a `(principal / project)`, **not** to a single Allocation. Groups the
  sequence of Runs; carries narrative/notes and rolled-up cost attribution.
- **May span System reprovisions, Allocations, and resource kinds**: if the chase
  moves from a local VM to bare metal — a new Allocation on a different Resource —
  the Investigation continues. Each Run records which System it used, and cost
  attribution **rolls up across allocations and `cost_class` boundaries**.

### Run

One kernel-version attempt: build patch vN → install → boot that kernel → debug
it.

- States: `created → running → succeeded / failed / canceled`.
- **Idempotent steps** keyed by `run_id` + step (the one PoC invariant kept).
  One build per Run keeps this clean.
- The agent's real loop is **many Runs against one persistent System**, each Run
  carrying at most one DebugSession (per boot). Allocation and provisioning
  happen once; iteration is cheap.

### DebugSession

A sub-object of a Run, bounded by a single boot of a single kernel.

- States: `attach → live → detached`.
- **A durable row**, not just worker-side state: persists `(state, transport
  handle, worker heartbeat)` so the reconciler can detect a `live` session whose
  transport has died and move it to `detached` (see Reconciliation & teardown).
- Within one boot it can cycle attach ↔ detach (and interrupt/continue) any
  number of times.
- A **reboot ends it**: the transport drops and, for a patched kernel, symbols
  and addresses change. The next attach after a reboot is a new DebugSession
  belonging to the next Run.

### Carried invariants (generalized from the PoC)

1. **Immutable request inputs** per object once created (the profiles that
   defined it).
2. **Idempotent, lock-guarded step execution** — Postgres row / advisory locks
   replace flock; serialization is per-Allocation and per-System.
3. **A Run's Allocation is determined by its System** (`run.system → allocation`).
   The Investigation grouping a Run imposes no allocation constraint — it may
   group Runs across different Allocations and resource kinds.

## Provider / capability model

Providers are the extension seam. A provider is a plugin that implements one or
more **plane interfaces** for a resource `kind`. Capabilities advertise what is
actually implemented — not every provider implements every plane.

Two rules:

- **Capability-based dispatch** — the core selects a provider by matching the
  requested operation against advertised capabilities; it never hardcodes
  provider names. Adding ppc64le/PowerVM is a new provider package with zero core
  change.
- **Each plane operation declares its contract** — idempotent? destructive?
  cancelable? long-running (job) vs synchronous? **and its cancel/abandon cleanup
  guarantee** (clean-rollback / best-effort / orphan-flagged). This drives
  job-queue routing, the destructive-op policy gate, and the reconciler (see
  Reconciliation & teardown).

## Lifecycle planes

| Plane | Responsibility | Local-libvirt (slice 1) | Later providers |
|---|---|---|---|
| Discovery | register resources, advertise capabilities, report health | enumerate local libvirt host | cloud regions, lab inventory, HMC frames |
| Allocation | claim/lease/release; feeds admission control + accounting | always-yes lease (capacity-checked) | cloud reserve API, lab reservation, LPAR activate |
| Provisioning | apply a provisioning profile → a ready System | libvirt XML + rootfs image | ISO+kickstart, golden/QCOW2 images, ansible, NIM/PXE |
| Build | produce a kernel from source + profile | local `make` | remote build host, GitHub Actions workflow |
| Install | deploy a built kernel onto a System | copy + direct-kernel boot | SSH push, image bake, netboot |
| Connect | establish a debug/console transport | QEMU gdbstub, SSH/serial | SoL, KGDB-over-serial, BMC console |
| Debug | constrained debug ops over a transport | gdb-MI + drgn | crash, KDB |
| Control | power/reset/force-crash | virsh destroy/reset/`sysrq-c` | IPMI/Redfish power, HMC, NMI |
| Retrieve | pull debug artifacts | vmcore via kdump path | remote vmcore fetch, BMC SOL capture |

**Ported from the PoC behind these interfaces:** redaction, path safety,
constrained-debug allowlist, gdb-MI tier, drgn introspect/vmcore, crash
postmortem, run-readiness preflight.

## MCP tool surface

Atomic primitives mapped to planes, plus thin orchestration conveniences. Every
tool returns structured JSON with the relevant object id, status,
`suggested_next_actions`, and artifact **references** — never log dumps.

**Long-running operations use an explicit job model.** Provision, build, install,
capture-vmcore can run 30+ minutes. Those tools enqueue a job and return
`{job_id, status: "running"}`; the agent polls `jobs.get` (or `jobs.wait` with a
timeout). Fast ops (set breakpoint, read memory, power state) return directly.

```
Discovery / selection
  resources.list(filter)              → resources + advertised capabilities
  resources.describe(resource_id)     → full capability detail, health, cost_class

Allocation                            (admission control + accounting)
  allocations.request(selector, window, project)  → granted | denied | job
  allocations.list / .get / .release
  accounting.estimate(selector)       → cost estimate before committing
  accounting.usage(project|principal) → ledger rollup, budget remaining

Provisioning
  systems.provision(allocation_id, provisioning_profile)   → job → system_id
  systems.list / .get / .reprovision / .teardown

Investigation + Run
  investigations.open(project, title)         → investigation_id
  runs.create(investigation_id, system_id, build_profile, …)
  runs.build(run_id)    → job        runs.install(run_id) → job
  runs.boot(run_id)     → job        runs.get(run_id)

Connect + Debug
  debug.start_session(run_id, transport)   debug.end_session
  debug.set_breakpoint / .clear / .list
  debug.continue / .interrupt
  debug.read_registers / .read_symbol / .read_memory(≤4096) / .evaluate(constrained)
  introspect.run / .from_vmcore         postmortem.crash / .triage

Control + Retrieve                    (destructive → policy gate)
  control.power(system_id, on|off|cycle|reset)
  control.force_crash(system_id)
  artifacts.list(run_id) / .get(ref)
  vmcore.list(system_id) / .fetch(system_id) → job

Jobs (long-running spine)
  jobs.get(job_id) / jobs.wait(job_id, timeout) / jobs.cancel(job_id) / jobs.list

Orchestration conveniences (thin wrappers)
  workflow.build_boot_debug(run_id)        workflow.build_boot_test(run_id)
```

- Workflow tools are optional sugar, not the only path — an agent can drive
  plane-by-plane, which matches how it iterates on a patch.
- `jobs.*` is the uniform async spine: every long-running tool returns the same
  job-handle shape, so the agent learns one polling pattern.
- `debug.read_memory` keeps the PoC's 4096-byte cap.

## Cross-cutting concerns

Applied across every plane.

- **Secrets by reference** — cloud creds, BMC/IPMI passwords, SSH keys, sudo,
  HMC tokens never appear in requests, state rows, or responses. The service
  resolves references from a pluggable secret backend at the worker boundary;
  only `(present, source-ref)` is persisted. When a worker resolves a reference,
  it **registers the resolved value into the redaction registry** (the ported
  `PROCESS_SECRET_REGISTRY.register`) for the op's lifetime, so any transcript or
  console output capturing the value is masked by **exact-value replacement**, not
  merely by the redactor's secret-name patterns. Output captured before
  registration completes is quarantined (object-store, sensitive) until redacted.
- **Mandatory redaction** — all guest output, gdb/SoL transcripts, and console
  logs pass through the redactor before persistence and before any response
  snippet. Raw artifacts stay in the object store, marked sensitive, fetched only
  by explicit `artifacts.get`.
- **Audit log** — every state transition and every destructive op writes an
  append-only audit row attributing `(principal, agent_session, tool,
  args-digest)`.
- **Accounting ledger** — allocation transitions emit usage events; admission
  control checks budget/quota on `allocations.request` and denies or requires
  approval over budget. The budget/quota **check and the resulting ledger debit
  are atomic** under a per-project lock (see Concurrency) — otherwise two
  concurrent requests can both pass the check and overspend.
- **Destructive-op policy gate** — `control.power(off/cycle/reset)`,
  `force_crash`, `teardown`, disk delete, and PCI passthrough are gated by three
  independent, all-required checks: (a) the allocation's granted capability
  scope, (b) RBAC role, (c) an explicit profile/flag opt-in.
- **Concurrency** — serialize per-Allocation and per-System via Postgres advisory
  locks; idempotent steps keyed by `run_id` + step. Admission control serializes
  on a **per-project (budget-scope) lock** — an advisory lock or `SELECT … FOR
  UPDATE` on the budget row — so the check-then-debit on `allocations.request`
  cannot race.

### Reconciliation & teardown

State in Postgres can drift from real infrastructure whenever a worker dies, a
lease expires mid-operation, or a `jobs.cancel` lands on a half-applied op. A
periodic **reconciler loop** in the core detects and repairs that drift:

- **Orphaned Systems** — a System whose Allocation is `released` / `expired` /
  `failed` is torn down (a System never outlives its Allocation).
- **Runs on torn-down Systems** — a Run whose System is torn down has its
  in-flight job canceled and the Run marked `failed` (`lease_expired`). The Run
  row is **retained, not deleted**, so the Investigation's cross-allocation
  narrative and cost rollup stay intact even though the Run's Allocation is gone.
- **Abandoned jobs** — each job carries a **worker heartbeat/lease**; when it
  lapses the job is marked abandoned and the op's declared compensation runs.
  (Advisory locks release on connection close and the PoC's `O_CREAT|O_EXCL` lock
  releases on unlink — but neither cleans up *infrastructure*, only the lock.)
- **Dead DebugSessions** — a session row in `live` whose transport is unreachable
  is moved to `detached`.
- **Leaked provider infra** — the reconciler reconciles against a provider
  `list-owned` / `reconcile` capability (Discovery plane) to find, e.g., a libvirt
  domain with no owning System row.

**Lease-expiry policy.** On `lease_expiry`, in-flight jobs are drained within a
grace window, then force-killed; the owning Run transitions to `failed`
(`lease_expired`) — distinct from a `canceled` Run, which records an explicit
`jobs.cancel` or agent abort, so audit and SLO tracking can tell an
infrastructure kill from a deliberate one. The accounting ledger attributes the
partial spend to the Allocation regardless of completion. **Cancel/abandon cleanup** is
part of every plane op's declared contract (see Provider / capability model):
each op declares whether cancel yields clean-rollback, best-effort, or
orphan-flagged state — `jobs.cancel` on a half-done `provision` / `install` is
never undefined.

## Error taxonomy

Keep the PoC's stable, agent-facing `ErrorCategory` taxonomy and extend it for
the new planes: `configuration_error`, `missing_dependency`, `build_failure`,
`boot_timeout`, `readiness_failure`, `test_failure`, `debug_attach_failure`,
`infrastructure_failure`, `stale_handle`, `transport_conflict`, `not_implemented`,
plus new categories — `allocation_denied` (admission/quota), `quota_exceeded`,
`lease_expired`, `provisioning_failure`, `install_failure`, `transport_failure`,
`control_failure`. Pick the most specific value; do not invent strings.

`stale_handle` and `transport_conflict` carry over from the PoC and matter *more*
in the distributed model: stale handles surface after a reprovision or reboot
invalidates a System/DebugSession reference; transport conflicts surface when two
attaches contend for one debug transport.

## Decomposition into sub-projects

Each gets its own spec → plan → implementation cycle.

1. **Core platform** — domain model, Postgres schema + repository layer, object
   store, job queue + worker tier, MCP/HTTP server skeleton, OIDC/RBAC, audit.
   (Foundation; everything depends on it.)
2. **Resource + Allocation plane** — discovery, capability model, admission
   control, accounting ledger, quotas/budgets.
3. **Provisioning plane** — provisioning-profile model + the libvirt provisioner.
4. **Build + Install plane** — local build, kernel install onto a System.
5. **Connect + Debug plane** — gdbstub/SSH transport, debug session lifecycle,
   ported gdb-MI/drgn/crash.
6. **Control + Retrieve plane** — virsh power/reset/force-crash, vmcore
   capture/fetch.

## Roadmap

Milestone-based. ("Sprint" is avoided per the project doc-style guard.)

- **M0 — Walking skeleton.** Core platform (#1) plus the thinnest path through
  every plane for **local-libvirt only**: request always-yes allocation
  (capacity-checked) → provision a libvirt System → build → install → boot → attach gdbstub → set
  breakpoint / read memory → force-crash → fetch vmcore. One resource kind, real
  end-to-end, on the new architecture. Proves the model and the seams.
- **M1 — Allocation/accounting depth.** Real reservation/lease semantics,
  admission control, ledger, quotas/budgets, OIDC/RBAC hardening. Still
  local-libvirt, but the allocation plane becomes real.
- **M1.5 — Fault-injection provider.** A mock provider behind the real plane
  interfaces that forces secret resolution and injects latency and failures
  (provision timeout, lease expiry mid-job, worker death, transport drop). It
  exercises reconciliation/teardown, the secret-registration contract, and
  admission-control races **before** any real remote provider — validating the
  seams while they are still cheap to change.
- **M2 — Remote libvirt.** Second provider behind the same interfaces — proves
  remote allocation/provision/install/transport with no core change.
- **M3 — Cloud.** Cloud provider + QCOW2/cloud-image provisioning + chargeback
  against real cost.
- **M4 — Bare metal.** PXE/SoL/IPMI/Redfish — the control plane gets real
  hardware power/crash.
- **M5 — PowerVM/ppc64le.** LPAR activation + HMC; second architecture.

Each milestone after M0 is intended to be "add a provider package + its
provisioning profiles," with the core and tool surface unchanged — the payoff of
the plane/capability design. **This is a falsifiable hypothesis, not a
guarantee**: the test is that adding the M2 remote provider touches zero lines in
`core/*` and the MCP tool-surface modules, measured by diff scope. M0 proves the
happy-path wiring end-to-end; it does **not** prove the seams hold under real
leasing, secret resolution, chargeback, or hardware failure — which is exactly
what the M1.5 fault-injection provider exists to stress first.

## Open follow-up decisions

Deferred to implementation planning / ADRs:

- Concrete job-queue technology (e.g. Postgres-backed queue vs Redis/Celery vs
  Temporal) and worker deployment shape.
- MCP Python server framework and streamable-HTTP auth integration specifics.
- Provisioning-profile schema (how libvirt XML / kickstart / ansible / QCOW2 are
  expressed under one model).
- Secret backend (file refs for M0; manager integration later).
- Object-store layout and retention policy for vmcores and transcripts.
- Migration/port plan for the salvaged PoC modules.
