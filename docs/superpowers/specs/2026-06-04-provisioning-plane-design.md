# Provisioning plane (libvirt) — Design

**Issue:** #16 (M0) · **Depends on:** #13 (capability registry / plane interfaces —
merged), #15 (provisioning-profile schema — merged), #9 (job queue/worker — merged),
#12 (reconciler — merged, enqueues `teardown`) · **Decisions:**
[ADR-0025](../../adr/0025-provisioning-plane-libvirt.md) (the decisions this spec
realizes), [ADR-0009](../../adr/0009-capability-provider-dispatch.md) (provider seam /
ordering), [ADR-0011](../../adr/0011-provisioning-profile-schema.md) /
[ADR-0024](../../adr/0024-provisioning-profile-model-shape.md) (profile shape),
[ADR-0018](../../adr/0018-job-queue-worker-execution.md) (job handler contract),
[ADR-0021](../../adr/0021-reconciler-loop-drift-repair.md) (row-first ordering, teardown),
[ADR-0023](../../adr/0023-discovery-allocation-admission.md) (synchronous-insert
precedent, the libvirt seam), [ADR-0019](../../adr/0019-tool-response-envelope.md)
(envelope), [ADR-0020](../../adr/0020-rbac-audit-gate-implementation.md) (RBAC/audit) ·
**Parent spec:** [`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md)
("systems.provision", the provisioning sequence diagram, "Domain objects in M0 → System",
exit criterion 2 & 5)

## Goal

The third plane of the walking skeleton: provision a `granted` Allocation into a running,
kdive-tagged libvirt System, and tear it down.

- `src/kdive/providers/local_libvirt/provisioning.py` — `LocalLibvirtProvisioning`, the
  `ProvisioningPlane` implementation over an **injected** libvirt connection: render the
  domain XML from a `ProvisioningProfile` (tagged with the System id in kdive metadata),
  `defineXML` + `create` it on `provision`, and `destroy` + `undefine` it idempotently on
  `teardown`. DB-free: it renders and drives libvirt, nothing else.
- `src/kdive/mcp/tools/systems.py` — the `systems.*` tool surface
  (`provision` / `get` / `teardown`) **and** the `provision` / `teardown` job handlers that
  orchestrate the System/Allocation state machine around the provider, plus
  `register(app, pool)` and `register_handlers(registry, *, provisioning=None)`.

Plus the minimal plumbing the above require:

- `src/kdive/domain/state.py` — add the `SystemState.PROVISIONING → TORN_DOWN` edge (so a
  stuck/abandoned mid-provision System tears down in one legal transition);
  `tests/domain/test_state.py`'s `LEGAL` table updated to match.
- `src/kdive/mcp/app.py` — append `systems.register` to `_PLANE_REGISTRARS` and
  `systems.register_handlers` to `_HANDLER_REGISTRARS`.

This layer sits **above** the repository/locks/RBAC/audit/job primitives and the
profile/discovery code, and **below** the agent. It owns *minting a System for an
Allocation*, *defining/tearing-down the libvirt domain*, and *the System read/lifecycle
tool surface*. It does **not** own building/installing a kernel (#17/#18), the
control/retrieve planes (#21/#22), or the reconciler loop (#12, already merged).

## Non-goals

- **No kernel build/install/boot, and no kdump reservation yet.** `provision` defines and
  starts a domain from the profile's `rootfs_image_ref`; staging the *built test kernel*,
  adding the direct-kernel `<kernel>`/`<cmdline>`, and applying the `crashkernel=` kdump
  reservation are the Install/Boot plane's (#17/#18 — the reservation is inert without
  `<kernel>`, so provision does not render it; see Components). A `ready` System is a
  defined+running libvirt domain, not yet running a kernel under test.
- **No reprovision-in-place.** M0 is one System per Allocation; the `defined` state and a
  `reprovisioning` path are M1 ([parent spec](../../specs/m0-walking-skeleton.md) "No
  System reprovision-in-place"). The System is born `provisioning` (ADR-0025 §1).
- **No reconciler wiring of a leaked-domain reaper.** #16 ships the `teardown` handler the
  reconciler's *orphaned-System* repair needs (it already enqueues `teardown` jobs) and the
  libvirt `destroy`/`undefine` op, but does **not** inject an `InfraReaper` into
  `__main__._run_reconciler` (still `NullReaper`); that operator wiring + its `live_vm`
  coverage is a later issue (ADR-0025 §8). The *leaked-domain* repair stays a no-op in
  production until then, unchanged from today.
- **No new ungated/`live_vm` test, and nothing un-gated.** Every provider behavior is
  covered with the injected `FakeLibvirtConn`; the real `libvirt.open` adapter and a true
  boot are `live_vm`-only. A real provision-against-libvirt acceptance needs the
  kdump-enabled rootfs-image fixture that lands with #24's `build-guest-image.sh`; #16 does
  not synthesize a phantom fixture, mirroring #14 ("adds no new ungated integration test").
- **No destructive-op gate on teardown.** `systems.teardown` requires `operator`, like
  `allocations.release`; the three-check gate is for `force_crash`/`power` (#21), not
  routine lifecycle cleanup (ADR-0025 §6).
- **No OCI image resolution.** `rootfs_image_ref` (an `oci://…@sha256:…` ref) is placed
  into the rendered domain disk source verbatim; pulling/converting the image to a libvirt
  volume is a `live_vm`-host concern (and a later hardening), not part of the unit-tested
  rendering. Stated so the absent pull is a recorded boundary, not an oversight.
- **No widening of `ProvisioningPlane` or `ToolResponse`.** The Protocol's
  `provision(alloc, profile) -> SystemHandle` / `teardown(system) -> None` shape is honored
  (M0 handles are `str` aliases); `ToolResponse.data` stays `dict[str, str]`.

## Components

### `provisioning.py` — the Provisioning plane (pure libvirt)

```python
_KDIVE_METADATA_NS = discovery._KDIVE_METADATA_NS   # the read-side contract, imported

type Connect = Callable[[], LibvirtConn]            # zero-arg; returns a live connection

SUPPORTED_DOMAIN_XML_PARAMS = frozenset({"machine"})

def domain_name_for(system_id: UUID) -> str: ...    # "kdive-{system_id}"
def validate_profile(profile: ProvisioningProfile) -> None: ...   # raises CONFIGURATION_ERROR on unknown domain_xml_params
def render_domain_xml(system_id: UUID, profile: ProvisioningProfile) -> str: ...

class LocalLibvirtProvisioning:
    def __init__(self, *, connect: Connect) -> None: ...
    @classmethod
    def from_env(cls) -> LocalLibvirtProvisioning: ...     # lambda: libvirt.open(KDIVE_LIBVIRT_URI)
    def provision(self, system_id: UUID, profile: ProvisioningProfile) -> str: ...   # -> domain_name
    def teardown(self, domain_name: str) -> None: ...
```

- **The libvirt seam.** `connect` is the same zero-arg-callable pattern as discovery; the
  only methods used are `defineXML(xml)`, the returned domain's `create()`, and lookup
  (`lookupByName(name)`) → `destroy()` / `undefine()`. `from_env` builds
  `lambda: libvirt.open(host_uri)` from `KDIVE_LIBVIRT_URI` (default `qemu:///system`),
  carrying the same scoped `ty: ignore[invalid-argument-type]` at the `libvirt.open` seam
  that discovery carries. Unit tests inject a fake.
- **`render_domain_xml(system_id, profile)`** builds, with `xml.etree.ElementTree`, a
  minimal `<domain type='kvm'>`:
  - `<name>kdive-{system_id}</name>`, `<uuid>` omitted (libvirt assigns one).
  - `<memory unit='MiB'>{profile.memory_mb}</memory>`, `<vcpu>{profile.vcpu}</vcpu>`.
  - `<os><type arch='{profile.arch}' machine='{domain_xml_params.get("machine", _DEFAULT_MACHINE)}'>hvm</type></os>`.
    **No `<kernel>`/`<cmdline>` at provision.** `boot_method` is `direct-kernel` (the only M0
    value), but libvirt only passes `<os><cmdline>` when a `<kernel>` direct-boot element is
    present, and the *test kernel* is not built/installed until #17. Rendering a `crashkernel=`
    cmdline here would be **inert** (libvirt ignores `<cmdline>` without `<kernel>`), a phantom
    reservation. So the kdump `crashkernel=` reservation is the Install/Boot plane's job (#17),
    applied to the direct-kernel `<cmdline>` it adds with the built kernel; provision renders the
    domain shell + rootfs + metadata tag only. `provider.crashkernel` is carried on the stored
    profile (the System row) for #17 to consume; provision does **not** materialize it into XML.
    Stated so the absent reservation is a recorded boundary (#17 owns it), not a silent gap.
  - `<devices><disk type='file' device='disk'><source file='{provider.rootfs_image_ref}'/>
    <target dev='vda' bus='virtio'/></disk></devices>` — the rootfs ref verbatim (non-goal:
    no OCI resolution).
  - `<metadata><kdive:system xmlns:kdive='{_KDIVE_METADATA_NS}'>{system_id}</kdive:system></metadata>`
    — the tag discovery reads. **This is the contract**; a rendering test asserts that
    `discovery._parse_system_id` round-trips the value out of the rendered element.
  - **`domain_xml_params`** (ADR-0024 `dict[str,str]`): M0 honors a documented, closed set —
    `SUPPORTED_DOMAIN_XML_PARAMS = {"machine"}` (→ `<os><type machine=…>`; `machine` defaults
    to `_DEFAULT_MACHINE` when absent). An **unknown** key raises
    `CategorizedError(CONFIGURATION_ERROR)` — *fail loud*: a param the renderer would silently
    drop is a misconfiguration the operator must see. **The check runs at the tool boundary,
    not only at render time.** A module-level `validate_profile(profile)` checks the param keys
    against `SUPPORTED_DOMAIN_XML_PARAMS`; `systems.provision` calls it synchronously right
    after `ProvisioningProfile.parse`, so a bad param is an immediate `configuration_error`
    response, not a dead-lettered provision job (the codebase rule: validate at the boundary,
    fail fast). `render_domain_xml` re-applies the same check defensively (the worker boundary),
    so a hand-built jsonb that bypassed the tool still cannot inject an unknown key. The
    supported-set widening is an M1 concern; both check sites share the one constant.
- **`provision(system_id, profile)`** renders the XML, `conn.defineXML(xml)` → domain,
  `domain.create()` (start it), returns `domain_name_for(system_id)`. It is **idempotent**:
  `defineXML` redefines an existing domain, and a `create()` reporting the domain is *already
  running* (`VIR_ERR_OPERATION_INVALID`) is the desired post-state, swallowed — so a handler
  retry after a finalize that failed *post-create* does not mark a running System failed (the
  same idempotency teardown has). Any other `libvirtError` is raised as
  `CategorizedError(PROVISIONING_FAILURE, details={"system_id": …})` (no domain leaks a row-less,
  untagged artifact — the row already exists, decision 1, and the domain, if defined, carries the
  tag so the reconciler can reap it). The connection is closed in a `finally` (no per-job leak).
- **`teardown(domain_name)`** is idempotent (ADR-0025 §5): `lookupByName` →
  `VIR_ERR_NO_DOMAIN` means already gone → return; else `destroy()` (ignore
  `VIR_ERR_OPERATION_INVALID` = not running) then `undefine()` (ignore `VIR_ERR_NO_DOMAIN`).
  Any other libvirt error → `CategorizedError(INFRASTRUCTURE_FAILURE, details={"domain": …})`.

### `systems.py` — the `systems.*` tools and the job handlers

```python
async def provision_system(pool, ctx, *, allocation_id, profile) -> ToolResponse: ...
async def get_system(pool, ctx, system_id) -> ToolResponse: ...
async def teardown_system(pool, ctx, system_id) -> ToolResponse: ...

async def provision_handler(conn, job, provisioning: LocalLibvirtProvisioning) -> str | None: ...
async def teardown_handler(conn, job, provisioning: LocalLibvirtProvisioning) -> str | None: ...

def register(app: FastMCP, pool: AsyncConnectionPool) -> None: ...
def register_handlers(registry: HandlerRegistry, *, provisioning: LocalLibvirtProvisioning | None = None) -> None: ...
```

#### `systems.provision(allocation_id, profile)` — synchronous mint + enqueue

`profile` is the raw profile document (a JSON object the agent supplies).

1. `require_project` is implicit via the allocation's project (resolved below);
   `require_role(ctx, project, Role.OPERATOR)` after the project is known.
2. Parse the uuid (malformed → `failure(CONFIGURATION_ERROR)`); `profile = ProvisioningProfile.parse(profile)`
   then `provisioning.validate_profile(profile)` (a bad profile or an unknown
   `domain_xml_params` key raises `CategorizedError(CONFIGURATION_ERROR)` → mapped to `failure`,
   details already value-scrubbed by `parse`). Both run **before** any DB write, so a
   misconfigured profile never mints a System or enqueues a job.
3. One transaction under `advisory_xact_lock(ALLOCATION, allocation_id)`:
   - `ALLOCATIONS.get`; absent or `alloc.project not in ctx.projects` →
     `failure(CONFIGURATION_ERROR)` (not-found-shaped, no cross-project leak — as
     `allocations.get`). `require_role(ctx, alloc.project, OPERATOR)`.
   - **Find existing System for this allocation** (`SELECT … FROM systems WHERE
     allocation_id = %s`). If one exists:
     - **terminal (`torn_down`/`failed`)** → `failure(CONFIGURATION_ERROR,
       data={"current_status": …})`: the allocation's System is spent and M0 has **no
       reprovision** ([parent spec](../../specs/m0-walking-skeleton.md) "No System
       reprovision-in-place") — the agent must release this allocation and request a new one.
       Re-enqueuing the (terminal) provision job here would hand back a stale succeeded/failed
       handle that misrepresents a fresh provision.
     - **non-terminal (`provisioning`/`ready`/`crashed`)** → a genuine retry: re-enqueue the
       `provision` job (dedup → the same job) and return its handle — **no** second System,
       **no** re-transition. This is the tool's idempotency (ADR-0025 §2).
   - Else require `alloc.state is GRANTED` (any other state →
     `failure(CONFIGURATION_ERROR, data={"current_status": …})`: a released/active/failed
     allocation cannot start a *new* System). Then:
     - `SYSTEMS.insert(System(state=PROVISIONING, allocation_id, provisioning_profile=
       profile.model_dump(by_alias=True), domain_name=None, principal/agent_session/project
       from ctx))`; audit `"->provisioning"`.
     - `ALLOCATIONS.update_state(GRANTED → ACTIVE)`; audit `"granted->active"`.
     - `queue.enqueue(PROVISION, {"system_id": str(system.id)}, authorizing=<ctx tuple>,
       dedup_key=f"{allocation_id}:provision")`.
4. Return the job-handle envelope. `ToolResponse.from_job` hardcodes `data={"kind": …}`
   (responses.py), so the tool **builds the envelope directly** — same `object_id`/`status`/
   `suggested_next_actions`/`refs` as `from_job`, but `data={"kind": job.kind.value,
   "system_id": str(system.id)}` — so a single `jobs.wait` plus the carried id lets the agent
   `systems.get`. A small `_job_envelope(job, system_id)` helper in `systems.py` keeps the
   "category iff failure" discipline (it goes through `ToolResponse(...)` like `from_job`).

`IllegalTransition` from the `granted → active` step is caught as a backstop (a race the
lock did not cover) → `failure(CONFIGURATION_ERROR, data={"current_status": …})` re-read on
a fresh connection, the `allocations.release`/`jobs.cancel` pattern. No `IllegalTransition`
reaches the transport.

#### `provision` handler — the libvirt work

`(conn, job, provisioning)`:

1. `system_id = UUID(job.payload["system_id"])`; `system = SYSTEMS.get(conn, system_id)`.
   Absent → raise `CategorizedError(INFRASTRUCTURE_FAILURE)` (the row the tool wrote is
   gone — a consistency error; dead-letter it).
2. If `system.state` is `READY` → return `str(system_id)` (idempotent retry: the domain is
   already up). If terminal (`torn_down`/`failed`) → return (nothing to do; a teardown/failure
   raced ahead). Only `provisioning` proceeds.
3. `profile = ProvisioningProfile.parse(system.provisioning_profile)`.
4. `try: domain_name = provisioning.provision(system_id, profile)` —
   `except CategorizedError`: `SYSTEMS.update_state(system_id, PROVISIONING → FAILED)` then
   re-raise (the worker dead-letters the job with `PROVISIONING_FAILURE`; the System reads
   `failed`).
5. One transaction under `advisory_xact_lock(SYSTEM, system_id)`: **re-read** the System
   state under the lock (`FOR UPDATE`). If still `provisioning` → set state+domain_name
   (`UPDATE systems SET state='ready', domain_name=…`), audit `"provisioning->ready"`, return
   `str(system_id)`. If it is **no longer `provisioning`**, branch on *why* (outside the lock):
   - **terminal (`torn_down`/`failed`)** → a concurrent `teardown` (or a failed sibling)
     superseded this provision; **tear down the domain just created**
     (`provisioning.teardown(domain_name)`, idempotent) and return without a transition — not an
     error (no dead-letter).
   - **`ready`/`crashed`** → a concurrent **same-job** provision (a lease-lapse double-run)
     already finalized this System; that domain is the live System's, so **leave it** and return.
     Tearing it down here would destroy a running System. This is the one case the naive "any
     non-`provisioning` re-read ⇒ teardown" gets wrong.

The handler holds **no** transaction across either libvirt call (ADR-0018): `provision` and
the cleanup `teardown` run outside any txn; the lock-held section is only the fast re-read +
transition, and the slow libvirt calls are unlocked — the reconciler's own
`_repair_leaked_domains` pattern (lock the guard, destroy unlocked).

**Audit attribution in handlers (ADR-0025 §9).** A handler holds a `Job`, not a
`RequestContext`, but `audit.record` requires one and *guards* `project in ctx.projects`
(audit.py). The handler reconstructs the ctx from the job's authorizing tuple and the
System's project: `RequestContext(principal=job.authorizing["principal"],
agent_session=job.authorizing.get("agent_session"), projects=(system.project,), roles={})`.
The project is the System's own, so it is in the singleton `projects` and the guard passes —
including for a reconciler-enqueued teardown whose principal is `system:reconciler`. The
transition row is attributed to whichever caller *enqueued first* (dedup coalescing keeps one
`authorizing` tuple); an operator teardown that coalesces onto a reconciler GC job audits as
`system:reconciler`, and vice versa. This is acceptable — the audit records the transition and
a legitimate authorizer; the structured log carries the live actor. The handler audits **each
transition it commits** — `provisioning->ready` on success, `provisioning->failed` on a provider
error (committed in step 4 before the re-raise, so the audit reflects the real state change even
as the job dead-letters), and `<old>->torn_down` on teardown — one row per committed transition,
honoring the #9 "every transition audits" invariant for the transitions a handler actually
drives. (The reconciler's own GC transitions are raw-SQL and un-audited by design, ADR-0021; the
handler is the audited path.)

#### `systems.teardown(system_id)` — enqueue idempotent teardown

1. Parse uuid; `SYSTEMS.get`; absent or cross-project → `failure(CONFIGURATION_ERROR)`.
   `require_role(ctx, system.project, OPERATOR)`.
2. If `system.state is TORN_DOWN` → `success(system_id, "torn_down")` (already done;
   idempotent, no job). Else `queue.enqueue(TEARDOWN, {"system_id": …}, authorizing=<ctx>,
   dedup_key=f"{system_id}:teardown")` — **the same dedup key the reconciler uses**, so an
   operator teardown and a reconciler GC teardown coalesce to one job.
3. Return `ToolResponse.from_job(job)` with `system_id` in `data`.

#### `teardown` handler — destroy + undefine + `→ torn_down`

`(conn, job, provisioning)`:

1. One transaction under `advisory_xact_lock(SYSTEM, system_id)`: `system = SYSTEMS.get`.
   Absent → return (nothing to tear down). Read `domain_name = system.domain_name or
   domain_name_for(system_id)` (a System torn down *before* `provision` set `domain_name` still
   has a deterministically-named domain to reap). **Transition only if not already terminal:**
   if `state is not TORN_DOWN`, `SYSTEMS.update_state(→ TORN_DOWN)` and audit `"<old>->torn_down"`;
   if already `torn_down`, skip the transition (no second audit row). Every non-terminal System
   state reaches `torn_down` directly — `ready`/`crashed`/`failed → torn_down` already exist, and
   **this issue adds `provisioning → torn_down`** (see "Domain/state change" below) so a stuck or
   abandoned mid-provision System tears down in one legal transition. Recording the state
   **before** the libvirt destroy (under the lock) is what makes the provision/teardown race
   safe: a concurrent `provision` re-reads `torn_down` under the same lock (provision step 5) and
   cleans up the domain it created.
2. `provisioning.teardown(domain_name)` **outside the lock**, **unconditionally** (idempotent;
   "already gone" is a no-op success). Return `str(system_id)`. Running the destroy after the
   committed `→ torn_down` — and running it *even when the row was already `torn_down`* — is what
   lets a teardown whose destroy **failed after** the state commit (dead-lettered, then requeued)
   recover on retry: the retry re-reads `torn_down`, skips the transition, but re-attempts the
   destroy. Without the unconditional destroy a post-commit destroy failure would leak the domain
   until the (deferred) leaked-domain reaper ran. The reaper remains the backstop for a System
   whose *row* is gone entirely.

#### Registration

`register(app, pool)` wires the three `@app.tool` wrappers (`systems.provision`/`.get`/
`.teardown`) over the handlers, resolving `current_context()` — the `allocations.py`
pattern. `register_handlers(registry, *, provisioning=None)` builds
`provisioning or LocalLibvirtProvisioning.from_env()` once and registers two closures
binding it into `JobKind.PROVISION` / `JobKind.TEARDOWN`. `app.py` appends `systems.register`
to `_PLANE_REGISTRARS` and `systems.register_handlers` to `_HANDLER_REGISTRARS`. Building the
provider in `register_handlers` does **not** open a libvirt connection (the `connect` lambda
is lazy), so the worker still boots without a reachable host; the first `provision`/`teardown`
job is the first connection.

### Domain/state change this requires: add `provisioning → torn_down`

The committed `SystemState` table has `PROVISIONING → {READY, FAILED}` only. But a `teardown`
can legitimately target a System still in `provisioning` — an orphaned-System GC (the
reconciler enqueues teardown for a `provisioning` System whose allocation released), or an
operator tearing down a stuck provision. The table must let teardown reach `torn_down` from
`provisioning`.

This issue **adds the `provisioning → torn_down` edge** (and updates
`tests/domain/test_state.py`'s hand-transcribed `LEGAL` table in the same commit, so the
parametrized legal/illegal suite stays the spec's executable mirror). This mirrors
[ADR-0023](../../adr/0023-discovery-allocation-admission.md) §5 exactly: that decision *added*
`granted → releasing` so an admitted-but-unprovisioned Allocation could be released from a
synchronous pre-terminal state — the identical shape of problem (a synchronously-created object
must be terminable before it advances). The new edge is additive and bisectable: it removes no
existing transition and needs no migration (the `systems_state_check` constraint already lists
`torn_down` as a legal value; only the in-code guard table gains an edge).

The rejected alternative — drive `provisioning → failed → torn_down` in the teardown handler —
was discarded because it writes a **false `failed`**: a System the operator deliberately tore
down, or a healthy-but-abandoned System the reconciler GCs, would be stamped with the
System-failure signal it never earned, polluting failure analytics. The single legal edge is
both cleaner and consistent with how this codebase already solved the same problem (ADR-0025 §5).

`SystemState.FAILED → TORN_DOWN` and `READY/CRASHED → TORN_DOWN` are unchanged and still apply
(a genuinely-failed provision still tears down from `failed`).

## Threat model & guarantees

- **Row-first ordering holds (no reaped mid-provision domain).** The `systems` row is
  written `provisioning` by the tool **before** the handler's `defineXML`; the reconciler's
  leaked-domain guard (a) finds it and skips (already pinned by
  `test_mid_provision_domain_not_reaped`). #16 adds no path that defines a domain before its
  row exists.
- **One System per Allocation, idempotent provision.** The tool finds an existing System for
  the allocation before inserting; a retried `systems.provision` returns the same job and
  never double-mints. The `provision` dedup key (`{allocation_id}:provision`) makes the job
  side idempotent too.
- **A System never outlives its Allocation.** The reconciler enqueues a `teardown` job for an
  orphaned System (allocation `released`/`failed`); #16's `teardown` handler now executes it
  (destroy+undefine, `→ torn_down`). The end-to-end invariant is live, not just enqueued.
- **Provision/release do not race.** Both take `advisory_xact_lock(ALLOCATION, id)`; a
  release that wins leaves the allocation non-`granted`, so a subsequent provision refuses;
  a provision that wins leaves an `active` allocation a later release can drive to `released`
  (orphaning the System → reconciler teardown).
- **Provision/teardown do not leak a domain.** The two handlers serialize their state
  decision on `advisory_xact_lock(SYSTEM, id)` (the slow libvirt call stays outside the lock,
  as in the reconciler). The teardown handler commits `→ torn_down` under the lock *before*
  destroying; a concurrent provision, after creating the domain, re-reads the System under the
  same lock and — seeing `torn_down` — tears down the domain it just created instead of
  setting `ready`. So a release-mid-provision cannot strand a tagged, running domain on a
  `torn_down` row, even with two workers; the DB invariant (`System torn_down`) and the
  host (no orphaned domain) stay consistent without depending on the deferred leaked-domain
  reaper. Provision's `provisioning → ready` is never applied to a System another actor drove
  terminal.
- **No injection through profile values.** Domain XML and the metadata tag are built with
  `ElementTree`; a `domain_xml_params`/ref value cannot escape its element.
- **Nothing secret flows here, so nothing is redacted.** The profile is by-reference and
  secret-free by contract (ADR-0011: no inline secrets); the rendered XML and stored profile
  carry only operator config and refs. Guest-output/secret redaction is the debug/retrieve
  planes' concern (#19/#22), not provisioning. Audit rows store an args digest, never the
  raw profile.
- **Teardown is safe to retry from any caller.** "Already gone" is success; the operator and
  reconciler GC coalesce on one dedup key; `→ torn_down` is idempotent.

## Failure modes & edges (drives the tests)

**provider** (`FakeLibvirtConn` + a `FakeDomain` that records `create`/`destroy`/`undefine`
calls; no DB, no `live_vm`)
- `render_domain_xml`: name `kdive-{id}`; memory/vcpu/arch from the profile; the
  `<kdive:system>` tag round-trips through `discovery._parse_system_id`; the rootfs ref in the
  disk source; `machine` from `domain_xml_params` (default when absent); **no `<kernel>`/
  `<cmdline>`** (the kdump reservation is #17's, not provision's).
- `validate_profile`: a profile whose `domain_xml_params` has only supported keys passes; an
  **unknown** key → `CategorizedError(CONFIGURATION_ERROR)`. `render_domain_xml` re-checks
  (the worker-boundary defense) so an unknown key in a hand-built jsonb still raises.
- `provision`: calls `defineXML` then `create`; returns `kdive-{id}`; a `libvirtError` on
  either → `CategorizedError(PROVISIONING_FAILURE)`.
- `teardown`: destroy+undefine a present domain; `VIR_ERR_NO_DOMAIN` on lookup → no-op
  success; `VIR_ERR_OPERATION_INVALID` on destroy (not running) → proceeds to undefine;
  another libvirt error → `CategorizedError(INFRASTRUCTURE_FAILURE)`.
- `from_env`: builds with `KDIVE_LIBVIRT_URI` (default `qemu:///system`); does not connect.

**provision handler** (real Postgres; fake provider injected)
- happy path: a `provisioning` System → provider `provision` called → System `ready`,
  `domain_name = kdive-{id}`; returns `str(id)`; one `provisioning->ready` audit row.
- idempotent retry: a `ready` System → provider **not** called again → returns; a terminal
  System → returns (no transition).
- provider failure: provider raises `PROVISIONING_FAILURE` → System `failed`, handler
  re-raises (worker dead-letters); the System reads `failed`, `domain_name` unset.
- missing row: payload `system_id` with no row → `INFRASTRUCTURE_FAILURE` (dead-letter).
- concurrent-teardown race (seed the System `torn_down` before step 5, e.g. drive it terminal
  between the provider call and the transition via a patched provider or a pre-seeded state):
  the handler does **not** set `ready` (no illegal transition escapes), tears down the
  just-created domain (provider `teardown` called with the deterministic name), and returns
  without error — no leaked domain, no dead-letter.

**teardown handler** (real Postgres; fake provider)
- `ready` System → provider `teardown(kdive-{id})` called → System `torn_down`.
- `provisioning` System (stuck) → `provisioning → torn_down` in one transition (the new
  edge); provider teardown called with the deterministic name.
- `failed` System → `failed → torn_down`; provider teardown called.
- already `torn_down` → no state transition (no second audit row), but the idempotent provider
  `teardown` **is** re-attempted (so a destroy that failed post-commit self-heals on retry).
- absent System → no-op (provider not called).
- a System whose `domain_name` is NULL (failed pre-rename) → teardown uses
  `domain_name_for(id)`; provider no-ops over an absent domain.

**`systems.provision` tool** (real Postgres; registered host; hand-built contexts)
- granted allocation + valid profile (operator) → `from_job` handle, `status="queued"`,
  `data["system_id"]` set; exactly one `systems` row `provisioning`; allocation `active`;
  one `provision` job (`dedup_key="{alloc}:provision"`); two audit rows (`->provisioning`,
  `granted->active`).
- retry (call twice, System still non-terminal) → same job id, one System, allocation still
  `active` (no second `granted->active`), no extra audit beyond the first call's.
- retry on a **terminal** existing System (seed `torn_down`/`failed` System on the allocation)
  → `failure(CONFIGURATION_ERROR, data.current_status=…)`; **no** new job, no stale handle (M0
  no-reprovision).
- non-`granted` allocation (released/active-without-system seeded) → `failure(CONFIGURATION_ERROR,
  data.current_status=…)`; no System, no job. *(active-with-existing-System is the retry case
  above.)*
- malformed uuid / bad profile (missing field) / unknown `domain_xml_params` key →
  `failure(CONFIGURATION_ERROR)` **synchronously** (no System, no job enqueued); profile error
  details carry no submitted values.
- viewer/no role → `AuthorizationError` raised (the `allocations` posture: authz denials are
  not envelopes, ADR-0020).
- cross-project / absent allocation → `failure(CONFIGURATION_ERROR)` (not-found-shaped).
- `IllegalTransition` backstop (monkeypatched `update_state`) → clean
  `failure(CONFIGURATION_ERROR, data.current_status=…)`, never a 500.

**`systems.get`** (real Postgres)
- own-project System → `success(id, state)`; cross-project / absent / malformed →
  `failure(CONFIGURATION_ERROR)` (indistinguishable, no leak).
- a `failed` System → `failure(INFRASTRUCTURE_FAILURE, data.current_status="failed")`
  (`"failed"` collides with the envelope's failure-status set — the `allocations`
  `_envelope_for_allocation` pattern); every other state → `success(id, state)`.

**`systems.teardown` tool** (real Postgres)
- a `ready` System (operator) → `from_job` handle; one `teardown` job
  (`dedup_key="{id}:teardown"`); `data["system_id"]` set.
- already `torn_down` → `success(id, "torn_down")`, **no** job enqueued.
- the dedup key matches a reconciler-enqueued teardown: an operator teardown after the
  reconciler enqueued one returns the **same** job (coalesced).
- cross-project / absent / malformed → `failure(CONFIGURATION_ERROR)`; viewer → `AuthorizationError`.

**app wiring**
- `build_app` registers `systems.provision`/`.get`/`.teardown` (assert via
  `app.list_tools()` carrying the dotted names, the `test_app` pattern).
- `build_handler_registry()` resolves a `provision` and a `teardown` handler
  (`registry.get(JobKind.PROVISION/TEARDOWN) is not None`); `register_handlers` with an
  injected fake provider registers without connecting.

**reconciler regression (must stay green)**
- the existing `tests/reconciler/test_loop.py` suite is unchanged and still passes — in
  particular `test_mid_provision_domain_not_reaped`, `test_orphaned_system_enqueues_gc_teardown`,
  and `test_torn_down_row_with_inflight_teardown_not_reaped`. #16 adds the *handler* for the
  job the reconciler enqueues; it does not alter the reconciler.

## Resolved decisions (carried from ADR-0025, pinned before code)

1. **System born `provisioning`, not `defined`** (ADR-0025 §1) — the synchronous-insert
   precedent (ADR-0023 §4 allocations skip `requested`). Tested: the provision tool writes a
   `provisioning` row, never a `defined` one.
2. **Tool mints + flips allocation `active` + enqueues, atomically; handler does libvirt**
   (ADR-0025 §2). Tested: the idempotent-retry and provision/release-serialization edges.
3. **Add the `provisioning → torn_down` state edge** (not a `provisioning → failed → torn_down`
   two-step, which would write a false `failed`) — mirrors ADR-0023 §5's `granted → releasing`
   addition. See "Domain/state change" above. Tested: the new edge is legal, the old illegal
   ones stay illegal, and the teardown handler reaches `torn_down` from `provisioning` directly.
4. **Unknown `domain_xml_params` key fails loud** (`CONFIGURATION_ERROR` at render). The
   alternative — silently dropping unsupported knobs — hides a misconfiguration; rejected.
5. **No leaked-domain reaper wiring** (ADR-0025 §8) — precedent #14. The reconciler suite is
   the regression guard that the *enqueue* path still works; the *handler* makes it execute.

## Testing strategy

Handlers and the provider are the unit of testing (repo contract): call
`render_domain_xml` / `LocalLibvirtProvisioning.provision` / `.teardown`,
`provision_handler` / `teardown_handler`, and `provision_system` / `get_system` /
`teardown_system` **directly** with an injected `FakeLibvirtConn`-backed provider and
hand-built `RequestContext`s — never through MCP transport.

- **Provider** uses an extended `FakeLibvirtConn` (a `defineXML` that returns a recording
  `FakeDomain`, `lookupByName` honoring a domain registry, `create`/`destroy`/`undefine`
  recorded; `libvirtError` injection via the existing `libvirt_error(code)` helper in
  `tests/providers/local_libvirt/conftest.py`) — no libvirt, no `live_vm`.
- **Handlers / tools** use the testcontainers Postgres fixtures (`migrated_url`, the
  `asyncio.run(_run())` idiom) re-exported as in `tests/mcp/conftest.py`; allocations/systems
  are seeded via the existing repositories or `systems.provision` itself. Contexts are
  hand-built `RequestContext(...)` with explicit `roles`.
- Tests live in `tests/providers/local_libvirt/test_provisioning.py` (the provider) and
  `tests/mcp/test_systems_tools.py` (tools + handlers), mirroring the package layout — the
  same split as discovery (`tests/providers/local_libvirt/`) vs allocations (`tests/mcp/`).
- The shared `FakeLibvirtConn` in `tests/providers/local_libvirt/conftest.py` gains the
  define/lookup methods the provider needs; the discovery tests that use the current fake are
  unaffected (additive methods).
- No new gated/`live_vm` test; nothing here needs libvirt/gdb/drgn at run time, and a real
  provision acceptance waits on #24's rootfs-image fixture (non-goals).
