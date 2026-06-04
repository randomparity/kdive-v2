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

- `src/kdive/mcp/app.py` — append `systems.register` to `_PLANE_REGISTRARS` and
  `systems.register_handlers` to `_HANDLER_REGISTRARS`.

This layer sits **above** the repository/locks/RBAC/audit/job primitives and the
profile/discovery code, and **below** the agent. It owns *minting a System for an
Allocation*, *defining/tearing-down the libvirt domain*, and *the System read/lifecycle
tool surface*. It does **not** own building/installing a kernel (#17/#18), the
control/retrieve planes (#21/#22), or the reconciler loop (#12, already merged).

## Non-goals

- **No kernel build/install/boot.** `provision` defines and starts a domain from the
  profile's `rootfs_image_ref` with a `crashkernel=` reservation; staging the *built test
  kernel* and rebooting into it is the Install/Boot plane (#17/#18). A `ready` System is a
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

def domain_name_for(system_id: UUID) -> str: ...    # "kdive-{system_id}"
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
  - `<os><type arch='{profile.arch}' machine='{domain_xml_params.get("machine", _DEFAULT_MACHINE)}'>hvm</type></os>`
    with a `<cmdline>` carrying `crashkernel={provider.crashkernel}` (the kdump reservation).
    `boot_method` is `direct-kernel` (the only M0 value); the kernel/initrd path is filled
    by Install (#17) — provision sets the reservation and the rootfs, not the test kernel.
  - `<devices><disk type='file' device='disk'><source file='{provider.rootfs_image_ref}'/>
    <target dev='vda' bus='virtio'/></disk></devices>` — the rootfs ref verbatim (non-goal:
    no OCI resolution).
  - `<metadata><kdive:system xmlns:kdive='{_KDIVE_METADATA_NS}'>{system_id}</kdive:system></metadata>`
    — the tag discovery reads. **This is the contract**; a rendering test asserts that
    `discovery._parse_system_id` round-trips the value out of the rendered element.
  - **`domain_xml_params`** (ADR-0024 `dict[str,str]`): M0 honors a documented, closed set —
    `machine` (→ `<os><type machine=…>`). An **unknown** key raises
    `CategorizedError(CONFIGURATION_ERROR)` at render time (fail loud: a param the renderer
    silently drops is a misconfiguration the operator must see, not absorb). The supported
    set widening is an M1 concern; the closed-set check is pinned by a test.
- **`provision(system_id, profile)`** renders the XML, `conn.defineXML(xml)` → domain,
  `domain.create()` (start it), returns `domain_name_for(system_id)`. A `libvirtError` from
  either call is raised as `CategorizedError(PROVISIONING_FAILURE, details={"system_id": …})`
  (no domain leaks a row-less, untagged artifact — the row already exists, decision 1, and
  the domain, if defined, carries the tag so the reconciler can reap it).
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
   (a bad profile raises `CategorizedError(CONFIGURATION_ERROR)` → mapped to `failure`,
   details already value-scrubbed by `parse`).
3. One transaction under `advisory_xact_lock(ALLOCATION, allocation_id)`:
   - `ALLOCATIONS.get`; absent or `alloc.project not in ctx.projects` →
     `failure(CONFIGURATION_ERROR)` (not-found-shaped, no cross-project leak — as
     `allocations.get`). `require_role(ctx, alloc.project, OPERATOR)`.
   - **Find existing System for this allocation** (`SELECT … FROM systems WHERE
     allocation_id = %s`). If one exists (a retry): re-enqueue the `provision` job (dedup →
     the same job) and return its handle — **no** second System, **no** re-transition. This
     is the tool's idempotency (ADR-0025 §2).
   - Else require `alloc.state is GRANTED` (any other state →
     `failure(CONFIGURATION_ERROR, data={"current_status": …})`: a released/active/failed
     allocation cannot start a *new* System). Then:
     - `SYSTEMS.insert(System(state=PROVISIONING, allocation_id, provisioning_profile=
       profile.model_dump(by_alias=True), domain_name=None, principal/agent_session/project
       from ctx))`; audit `"->provisioning"`.
     - `ALLOCATIONS.update_state(GRANTED → ACTIVE)`; audit `"granted->active"`.
     - `queue.enqueue(PROVISION, {"system_id": str(system.id)}, authorizing=<ctx tuple>,
       dedup_key=f"{allocation_id}:provision")`.
4. Return `ToolResponse.from_job(job)` with `system_id` added to `data` (so a single
   `jobs.wait` + the carried id lets the agent `systems.get`). `from_job` already sets
   `suggested_next_actions` by job state; the System read is reachable via `data["system_id"]`.

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
5. One transaction: set `domain_name` (raw `UPDATE systems SET domain_name=%s`) and
   `SYSTEMS.update_state(system_id, PROVISIONING → READY)`; audit `"provisioning->ready"`
   under the `system:worker`/authorizing attribution carried in the job. Return `str(system_id)`.

The handler holds **no** transaction across the libvirt call (ADR-0018): the long
`provision` runs outside any txn, the DB writes are their own short transactions.

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

1. `system = SYSTEMS.get(conn, system_id)`. Absent → return (nothing to tear down). If
   `TORN_DOWN` → return (idempotent).
2. `domain_name = system.domain_name or domain_name_for(system_id)` — a System that failed
   *before* `provision` set `domain_name` still has a deterministically-named domain to
   reap (or none, which `teardown` no-ops over).
3. `provisioning.teardown(domain_name)` (idempotent; "already gone" is success).
4. `SYSTEMS.update_state(system_id, → TORN_DOWN)` if the current state allows it
   (`ready`/`crashed`/`provisioning`/`failed` → `torn_down`; **`provisioning → torn_down`
   is not a legal edge** — see "Domain/state" below). Audit `"<old>->torn_down"`. Return
   `str(system_id)`.

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

### State-machine gap this surfaces: `provisioning → torn_down`

The committed `SystemState` table has `PROVISIONING → {READY, FAILED}` only — **no**
`provisioning → torn_down`. But a `teardown` can legitimately target a System still in
`provisioning` (an orphaned-System GC, or an operator tearing down a stuck provision). Two
ways to honor the teardown contract without an illegal transition:

- **(chosen)** the `provision` handler drives a failed provision to `failed` first
  (decision: a provision that will be torn down has already failed or will), and the
  `teardown` handler maps the *reachable* pre-teardown states (`ready`, `crashed`, `failed`)
  → `torn_down`. For a System genuinely stuck in `provisioning` with no failure yet (e.g. the
  provision job never ran), the `teardown` handler first transitions `provisioning → failed`
  (a legal edge) **then** `failed → torn_down` (legal), so teardown is always reachable
  without widening the table. This two-step is documented in the handler and tested.

This keeps the state table unchanged (no migration, no edge addition) and is the minimal
honest reading of "teardown is idempotent and always reaches `torn_down`".

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
  `<kdive:system>` tag round-trips through `discovery._parse_system_id`; `crashkernel=` on
  the cmdline; the rootfs ref in the disk source; `machine` from `domain_xml_params`
  (default when absent); an **unknown** `domain_xml_params` key → `CategorizedError(CONFIGURATION_ERROR)`.
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

**teardown handler** (real Postgres; fake provider)
- `ready` System → provider `teardown(kdive-{id})` called → System `torn_down`.
- `provisioning` System (stuck) → `provisioning → failed → torn_down`; provider teardown
  called with the deterministic name.
- already `torn_down` → no-op (provider not called, no transition).
- absent System → no-op.
- a System whose `domain_name` is NULL (failed pre-rename) → teardown uses
  `domain_name_for(id)`; provider no-ops over an absent domain.

**`systems.provision` tool** (real Postgres; registered host; hand-built contexts)
- granted allocation + valid profile (operator) → `from_job` handle, `status="queued"`,
  `data["system_id"]` set; exactly one `systems` row `provisioning`; allocation `active`;
  one `provision` job (`dedup_key="{alloc}:provision"`); two audit rows (`->provisioning`,
  `granted->active`).
- retry (call twice) → same job id, one System, allocation still `active` (no second
  `granted->active`), no extra audit beyond the first call's.
- non-`granted` allocation (released/active-without-system seeded) → `failure(CONFIGURATION_ERROR,
  data.current_status=…)`; no System, no job. *(active-with-existing-System is the retry case
  above.)*
- malformed uuid / bad profile (missing field) → `failure(CONFIGURATION_ERROR)`; profile
  error details carry no submitted values.
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
3. **`provisioning → torn_down` via `provisioning → failed → torn_down`** in the teardown
   handler (no state-table widening) — see "State-machine gap" above. Tested explicitly.
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
