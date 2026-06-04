# ADR 0025 — Provisioning plane: System creation & teardown on local libvirt (M0)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** #16 (M0: Provisioning plane (libvirt))
- **Depends on:** [ADR-0009](0009-capability-provider-dispatch.md) (provider seam /
  ordering), [ADR-0011](0011-provisioning-profile-schema.md) /
  [ADR-0024](0024-provisioning-profile-model-shape.md) (profile shape),
  [ADR-0018](0018-job-queue-worker-execution.md) (job queue / handler contract),
  [ADR-0021](0021-reconciler-loop-drift-repair.md) (reconciler ordering & teardown),
  [ADR-0023](0023-discovery-allocation-admission.md) (admission, the synchronous-insert
  precedent), [ADR-0019](0019-tool-response-envelope.md) (response envelope),
  [ADR-0020](0020-rbac-audit-gate-implementation.md) (RBAC/audit/gate)
- **Refines:** the M0 provisioning wording in
  [`../specs/m0-walking-skeleton.md`](../specs/m0-walking-skeleton.md) ("systems.provision",
  the provisioning sequence diagram, "Domain objects in M0 → System")

## Context

Issue #16 wires the third plane of the walking skeleton: **Provisioning** — turn a
`granted` Allocation into a running libvirt System, and tear it back down. The durable
`System` model and its `defined → provisioning → ready → … → torn_down` machine, the
`provision`/`teardown` `JobKind`s, the `ProvisioningPlane` Protocol, the
`ProvisioningProfile` model, the job queue/worker, the response envelope, and the
reconciler (which already *enqueues* `teardown` jobs and reaps leaked domains) all exist.
#16 adds: the libvirt provider that defines/starts/destroys a tagged domain, the
`systems.*` tool surface, and the `provision`/`teardown` job handlers that orchestrate
the DB state machine around the provider calls.

Eight decisions are either unpinned by the parent spec or pinned in a way that does not
survive scrutiny; they are settled here.

## Decision

### 1. The System row is inserted directly as `provisioning`; `defined` is not materialized

The `SystemState` machine starts at `defined`, but M0's `systems.provision` is a
synchronous tool that creates the row and enqueues the provisioning job in one step.
Exactly as admission inserts an Allocation **directly as `granted`** rather than
materializing the `requested` intermediate ([ADR-0023](0023-discovery-allocation-admission.md)
§4, because admission is synchronous), `systems.provision` inserts the System **directly
as `provisioning`**. The issue body ("Create the `systems` row (`provisioning`) **first**")
and the parent spec's sequence diagram ("insert System (provisioning) + enqueue provision
job") both state this; the acceptance line "drives `defined → provisioning → ready`"
describes the abstract machine, not the materialized rows (the same way the Allocation
machine names `requested` though no row is ever written in it). `defined` is reserved for
a future create-without-provision path (M1 reprovision); M0 never writes it.

The insert at `provisioning` is what makes the **row-first ordering** of
[ADR-0021](0021-reconciler-loop-drift-repair.md) hold: the non-terminal `systems` row
exists in Postgres **before** the libvirt domain is defined, so the reconciler's
leaked-domain guard (a) (`state <> 'torn_down'`) always finds a row for a mid-provision
domain and never reaps it (`tests/reconciler/test_loop.py::test_mid_provision_domain_not_reaped`
already pins this).

### 2. Synchronous tool, async handler: the tool owns the DB write, the handler owns libvirt

`systems.provision` does only fast, transactional work; the slow, fallible libvirt calls
run in the worker:

- **Tool (`systems.provision`)**, in one transaction under
  `advisory_xact_lock(ALLOCATION, allocation_id)`: validate the profile, **find-or-create**
  the System (insert at `provisioning`, storing the profile), transition the Allocation
  `granted → active`, enqueue the `provision` job (`dedup_key = "{allocation_id}:provision"`,
  payload `{"system_id": …}`), and audit both transitions. Returns the **job-handle**
  envelope (`ToolResponse.from_job`) carrying `system_id` in `data` so the agent can poll
  the job and then `systems.get`.
- **Handler (`provision`)**: read the System; if already `ready`/terminal, no-op (retry
  safety). Else call the provider to define+start the tagged domain, then in one
  transaction set `domain_name` and transition `provisioning → ready`. On a provider
  failure, transition `provisioning → failed` and raise `PROVISIONING_FAILURE` so the job
  dead-letters with the correct category and the System reflects the failure.

The **dedup key is the allocation, not the System**, because the System id is *minted* by
this operation — there is no System id to key on until the row exists, and "one System per
Allocation" (M0) makes the allocation the natural idempotency anchor. The tool is therefore
idempotent by *finding the existing System for the allocation* on a retry (rather than
inserting a second one and re-driving `granted → active`, which would raise
`IllegalTransition`); a retried `systems.provision` returns the same job handle.

Allocation `granted → active` is flipped **synchronously in the tool** (atomic with System
creation, under the allocation lock), not in the handler, so a concurrent
`allocations.release` serializes against it on the same lock: either release wins
(allocation `released`, provision then sees a non-`granted` allocation and refuses) or
provision wins (System created, allocation `active`, a later release drives
`active → releasing → released` and the orphaned System is torn down by the reconciler).
The allocation marks "a System exists on this host slot" the instant the row exists, even
if provisioning later fails — a `failed` System still occupies the slot until released.

### 3. Domain identity and the metadata tag are the discovery contract, built with ElementTree

The domain is named `kdive-{system_id}` and tagged with the libvirt metadata element
**discovery already reads** ([discovery.py](../../src/kdive/providers/local_libvirt/discovery.py)):
namespace `https://kdive.dev/libvirt/1`, element `<kdive:system>{system_id}</kdive:system>`.
This is the single source of truth tying a libvirt domain back to its `systems` row; the
reconciler's `list_owned`/leaked-domain repair depends on it. Both the domain XML and the
metadata element are assembled with `xml.etree.ElementTree` (structured construction), not
string interpolation, so a profile value can never break out of its element or inject XML.
The namespace constant is shared with discovery (imported, not re-declared) so the
write side and the read side cannot drift. The provision XML renders the domain shell,
the rootfs disk, and the metadata tag — but **no `<kernel>`/`<cmdline>`**: libvirt ignores
`<os><cmdline>` without a `<kernel>` direct-boot element, and the test kernel is not built
until Install (#17). So the kdump `crashkernel=` reservation is #17's to apply (to the
direct-kernel cmdline it adds), carried until then on the stored profile; rendering it at
provision would be an inert phantom reservation. Provision establishes the domain and the
rootfs, not the kernel under test.

### 4. The provider is a pure-libvirt seam with an injected connection factory; the handler orchestrates the DB

`LocalLibvirtProvisioning` mirrors `LocalLibvirtDiscovery`: a zero-arg `connect` callable
returning a libvirt-connection-like object (`from_env` builds
`lambda: libvirt.open(KDIVE_LIBVIRT_URI)`), exercised in unit tests with the existing
`FakeLibvirtConn`. It exposes two **DB-free** operations — `provision(system_id, profile)`
(render XML → `defineXML` → `create`, returning the domain name) and `teardown(domain_name)`
(`destroy` then `undefine`) — so every rendering/lifecycle behavior is covered without a
live host. The `provision`/`teardown` **handlers** (in `mcp/tools/systems.py`) own the
Postgres state machine and call the provider; the provider owns no Postgres, the handler
owns no XML. The real `libvirt.open` path is `live_vm`-gated, as for discovery.

### 5. `teardown` is idempotent and best-effort over an already-absent domain

A `teardown` job may run after the domain is already gone (a prior partial teardown, a
reconciler-reaped leak, or a crash mid-undefine). `teardown(domain_name)` therefore treats
"no such domain" (`VIR_ERR_NO_DOMAIN`) on `destroy`/`undefine` as success (the post-state —
domain absent — is already achieved), and a *running* vs *defined-but-stopped* domain is
handled by attempting `destroy` (ignore "not running", `VIR_ERR_OPERATION_INVALID`) before
`undefine`. Any other libvirt error is raised as `INFRASTRUCTURE_FAILURE`. The handler then
drives the System `→ torn_down` idempotently (already `torn_down` ⇒ no-op). This is what
makes both the operator path (`systems.teardown`) and the reconciler's GC path (the
`teardown` job it enqueues for an orphaned System) safe to retry — the reconciler's
leaked-domain repair relies on `destroy` being idempotent
(`tests/reconciler/test_loop.py::test_torn_down_row_with_inflight_teardown_not_reaped`).

`teardown` must also reach `torn_down` from a System still in `provisioning` (an
orphaned-System GC, or an operator tearing down a stuck provision). The committed state
table has `PROVISIONING → {READY, FAILED}` only, so this issue **adds the
`provisioning → torn_down` edge** rather than routing through `provisioning → failed →
torn_down`. The two-step would stamp a deliberately-torn-down (or healthy-but-abandoned)
System with the `failed` signal it never earned, polluting failure analytics; the single
additive edge avoids that. This mirrors [ADR-0023](0023-discovery-allocation-admission.md)
§5, which added `granted → releasing` for the identical shape of problem — a
synchronously-created object must be terminable before it advances. The edge is additive
(removes nothing, needs no migration: `systems_state_check` already lists `torn_down`), and
`tests/domain/test_state.py`'s `LEGAL` table is updated in the same commit.

### 9. Handlers reconstruct a `RequestContext` from the job's authorizing tuple to audit

A job handler holds a `Job`, not a `RequestContext`, but `audit.record` requires one and
guards `project in ctx.projects` (a misattribution backstop). The handler therefore builds
`RequestContext(principal=job.authorizing["principal"],
agent_session=job.authorizing.get("agent_session"), projects=(system.project,), roles={})`
before auditing a transition it commits — the project is the System's own, so the guard
passes, including for a reconciler-enqueued teardown whose principal is `system:reconciler`.
Because a `teardown` dedup-coalesces an operator request onto a reconciler GC job (or vice
versa), the audit row is attributed to whichever caller enqueued first; both are legitimate
authorizers and the structured log carries the live actor. The handler audits each
transition it commits (`provisioning->ready`, `provisioning->failed`, `<old>->torn_down`),
honoring the #9 "every transition audits" invariant for handler-driven transitions (the
reconciler's own GC transitions are raw-SQL and un-audited by design, ADR-0021).

### 6. `systems.teardown` requires `operator`; it is not behind the destructive-op gate

Teardown is the benign lifecycle counterpart to `systems.provision` and
`allocations.release` — the normal way a System ends — so it requires the `operator` role
(like `allocations.request`/`.release`), not the three-check destructive gate. The
destructive gate ([ADR-0020](0020-rbac-audit-gate-implementation.md)) is reserved for
`control.force_crash`/`power` (#21 in the plan), the operations that destroy *guest state
in place* on a System the agent means to keep debugging. Tearing a System down is not that;
gating it would make routine cleanup require `admin` + capability scope + profile opt-in,
which neither the spec nor the plan asks for.

### 7. The provisioning profile is stored on the System row by alias and re-parsed in the handler

The validated profile is persisted in `systems.provisioning_profile` (jsonb) via
`model_dump(by_alias=True)` so the provider section key round-trips as `local-libvirt` (its
wire alias), and the handler reconstructs it with `ProvisioningProfile.parse(...)` before
rendering XML. The job payload carries only the `system_id`; the profile travels on the row
(its system of record), so there is one stored copy and the handler renders from exactly
what was persisted. Re-parsing in the handler also re-asserts the schema at the
worker boundary rather than trusting a hand-built jsonb blob.

### 8. The leaked-domain reaper is not wired into the reconciler entrypoint here

#16 delivers the libvirt `destroy`/`undefine` operation and the `teardown` **handler**,
which closes the loop the reconciler's *orphaned-System* repair already opened (it enqueues
`teardown` jobs that, until now, had no handler and dead-lettered as `not_implemented`).
It does **not** compose a `list_owned`+`destroy` `InfraReaper` and inject it into
`__main__._run_reconciler` (still `NullReaper`). That wiring couples the reconciler process
to a reachable libvirt host and needs its own `live_vm` coverage; it is deferred to the
operator-wiring issue, exactly as #14 built `register_local_libvirt_resource` and
`list_owned` but left server-startup registration to a later issue
([ADR-0023](0023-discovery-allocation-admission.md) Consequences). The capability the
reconciler consumes (`teardown` of an orphaned System) is fully live after #16; the
*leaked-domain* repair (a domain whose row is gone entirely) stays a `NullReaper` no-op
until wired, unchanged from today.

## Consequences

- The walking-skeleton path gains `systems.provision`/`.get`/`.teardown`; provisioning is
  a job, so the agent polls it through the existing `jobs.*` surface.
- A mid-provision domain is never reaped (row-first ordering, decision 1), and a
  reconciler-enqueued `teardown` now executes (decision 5/8) — "a System never outlives its
  Allocation" is enforced end-to-end, not just enqueued.
- Provisioning is unit-tested without libvirt or a live host (decision 4); only the thin
  `libvirt.open` adapter and the real boot are `live_vm`-gated, and #16 adds no ungated
  integration test and un-gates nothing.
- `JobKind.PROVISION`/`TEARDOWN` gain handlers via the existing `_HANDLER_REGISTRARS` seam;
  no entrypoint edit beyond appending to that tuple and `_PLANE_REGISTRARS`.
- The metadata-tag namespace is now shared write-side (provisioning) and read-side
  (discovery); a change to it is a single-constant change with both sides' tests guarding it.
- The reconciler's *leaked-domain* repair remains dormant in production until a later issue
  injects a real reaper (decision 8); this is stated, not silently assumed.

## Alternatives considered

- **Insert the System as `defined`, then transition `defined → provisioning` in the tool.**
  Rejected: it materializes a `defined` row that exists for microseconds inside one
  transaction and writes a second audit row for a transition no observer can interleave on
  — the same reasoning that made admission skip `requested` (decision 1). The abstract
  machine keeps `defined` for the M1 create-without-provision path.
- **Create the System row inside the handler (key dedup on the System).** Rejected: there
  is no System id before the row exists, so the dedup key could not be `system_id`; and a
  tool that returns a job handle for a System the agent cannot yet `systems.get` is worse
  for the agent than synchronously minting the id. Keying on `allocation_id` also enforces
  one-System-per-Allocation idempotency directly.
- **Flip the Allocation `granted → active` in the handler on success.** Rejected: it leaves
  a window where a System row exists on a still-`granted` allocation, and a concurrent
  `allocations.release` would not serialize against provisioning (different lock timing),
  risking a released allocation with a live System the release never tore down. Flipping in
  the tool under the allocation lock closes that window.
- **Render domain XML by string formatting / a template.** Rejected: profile values
  (`domain_xml_params`, refs, arch) would be interpolated into markup, an injection seam;
  `ElementTree` construction makes that structurally impossible.
- **Put the destructive-op gate on `systems.teardown`.** Rejected (decision 6): teardown is
  routine lifecycle cleanup, not in-place guest destruction; gating it contradicts the plan,
  which gates only `force_crash`/`power`.
- **Wire a real `InfraReaper` into the reconciler in this issue.** Rejected for scope
  (decision 8): it adds a reconciler→libvirt deployment coupling and a `live_vm` test that
  belong with operator wiring; the precedent (#14 deferring registration wiring) applies.
- **Store the profile only in the job payload, not on the System row.** Rejected: the System
  row is the profile's system of record (the `provisioning_profile` column exists for it);
  duplicating it into the payload invites the two copies to disagree on a retry.
