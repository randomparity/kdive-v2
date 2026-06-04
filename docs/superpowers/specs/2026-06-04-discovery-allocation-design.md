# Discovery + Allocation (admission) — Design

**Issue:** #14 (M0) · **Depends on:** #13 (capability registry / plane interfaces —
merged), #11 (RBAC / audit / gate — merged) · **Decisions:**
[ADR-0023](../../adr/0023-discovery-allocation-admission.md) (the decisions this spec
realizes), [ADR-0004](../../adr/0004-first-slice-local-libvirt.md) (local-libvirt
slice), [ADR-0007](../../adr/0007-metering-budgets-admission.md) (admission control),
[ADR-0009](../../adr/0009-capability-provider-dispatch.md) (provider seam),
[ADR-0016](../../adr/0016-repository-layer-locks-idempotency.md) (advisory locks),
[ADR-0019](../../adr/0019-tool-response-envelope.md) (response envelope),
[ADR-0020](../../adr/0020-rbac-audit-gate-implementation.md) (RBAC/audit/gate) ·
**Parent spec:** [`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md)
("Local-libvirt provider", "Domain objects in M0 → Allocation", "MCP tool surface →
Discovery/Allocation", "Reconciler", exit criteria 1 & 5)

## Goal

The first two planes of the walking skeleton, wired onto the existing domain model:

- `src/kdive/providers/local_libvirt/discovery.py` — `LocalLibvirtDiscovery`, the
  `DiscoveryPlane` implementation: enumerate the local libvirt host (over an injected
  connection), advertise `arch`/`vcpus`/`memory_mb` + a `gdbstub` transport + the
  per-host `concurrent_allocation_cap`, and `list_owned()` the domains tagged with a
  `system_id`. Plus `register_local_libvirt_resource(...)`, the idempotent discovery→Postgres
  bridge that persists the host as the one `resources` row.
- `src/kdive/domain/allocation_admission.py` — `admit(...)`: take a per-resource
  advisory lock, count the host's non-terminal allocations against the cap, and either
  insert a `granted` Allocation (audited) or return an `allocation_denied` outcome with
  no row.
- `src/kdive/mcp/tools/resources.py` — `resources.list` / `resources.describe`.
- `src/kdive/mcp/tools/allocations.py` — `allocations.request` / `.get` / `.release` /
  `.list`.

Plus the minimal plumbing the above require:

- `src/kdive/domain/state.py` — add the `granted → releasing` Allocation edge (ADR-0023
  §5); `tests/domain/test_state.py` `LEGAL` table updated to match.
- `src/kdive/db/locks.py` — add `LockScope.RESOURCE` (ADR-0023 §1).
- `src/kdive/mcp/responses.py` — add `ToolResponse.success` / `.failure` factories
  (ADR-0023 §6).
- `src/kdive/mcp/app.py` — append `resources.register` and `allocations.register` to
  `_PLANE_REGISTRARS`.

This layer sits **above** the repository/locks/RBAC/audit primitives and **below** the
agent. It owns *discovering and registering the host*, *admitting allocations against a
per-host cap*, and *the read/allocate/release tool surface*. It does **not** own
provisioning (#15), the worker/job path (allocation is synchronous, not a job), or the
reconciler loop (separate issue).

## Non-goals

- **No provisioning, Systems, or teardown.** `allocations.request` grants capacity;
  `systems.provision` (#15) is what later transitions an allocation `granted → active`
  and creates a System. `allocations.release` in #14 transitions the **Allocation** to
  `released` only; it does **not** tear down a System (there are none yet). The
  parent-spec release line "released (System torn down)" is completed when provisioning
  lands and `release` gains the System-teardown step. Stated so the absent teardown is
  not read as a regression.
- **No budgets / spend / cost model.** Admission checks the per-host concurrent-count
  cap only (ADR-0007's per-project budget gate and its per-project lock are M1).
- **No job/worker involvement.** Discovery and admission are synchronous; no `jobs` row
  is created. (`force_crash`/`provision`/etc. are the job kinds, not allocation.)
- **No reconciler.** `list_owned()` is the *surface* the reconciler will consume; #14
  ships and tests it as a pure function over the libvirt connection, with no loop that
  reaps anything.
- **No live libvirt in the unit suite.** The real `libvirt.open` adapter is exercised
  only under the `live_vm` marker (CI deselects it); every behavior is covered with an
  injected fake connection. #14 adds **no** new ungated integration test and un-gates
  nothing.
- **No server-startup registration wiring.** `register_local_libvirt_resource` is a
  function tested directly; wiring it into an operator command/CLI is a later issue
  (ADR-0023 Consequences). The server must still boot without a reachable host.
- **No capability-scope population.** A granted Allocation's `capability_scope` is `{}`
  in M0 (the destructive-gate's `destructive_ops` key is populated by the
  provisioning/profile issue that owns it); `allocations.request` exposes no scope
  parameter.
- **No widening of `ToolResponse.data`.** It stays `dict[str, str]`; resources are
  projected to flat strings (ADR-0023 §6).

## Components

### `discovery.py` — the Discovery plane + registration bridge

```python
type Connect = Callable[[], LibvirtConn]   # zero-arg; returns a live connection

class LocalLibvirtDiscovery:
    def __init__(self, *, host_uri: str, connect: Connect, concurrent_allocation_cap: int) -> None: ...
    @classmethod
    def from_env(cls) -> LocalLibvirtDiscovery: ...     # reads KDIVE_LIBVIRT_URI + KDIVE_LIBVIRT_ALLOCATION_CAP
    def list_resources(self) -> list[ResourceRecord]: ...
    def list_owned(self) -> list[OwnedInfra]: ...

async def register_local_libvirt_resource(
    conn: AsyncConnection, discovery: LocalLibvirtDiscovery, *, pool: str, cost_class: str
) -> Resource: ...
```

- **The libvirt seam.** `connect` is a zero-arg callable returning a connection object;
  the only methods used are `getInfo()`, `getCapabilities()`, and `listAllDomains()`
  (and per-domain `name()` + `metadata(...)`). `from_env` builds the production
  connector `lambda: libvirt.open(host_uri)` and reads the cap from
  `KDIVE_LIBVIRT_ALLOCATION_CAP` (default `1`). `import libvirt` carries a per-site
  `# ty: ignore[unresolved-import]` (no stubs; pyproject note). Unit tests inject a fake
  connector; the real `open` is `live_vm`-only.
- **`list_resources()`** opens a connection and returns **one** `ResourceRecord` for the
  host:
  - `resource_id = host_uri` — the host's natural identity *before* it has a Postgres
    uuid (registration maps `host_uri → resources.id`). Documented as the
    discovery-time id; the persisted uuid is authoritative thereafter.
  - `kind = "local-libvirt"`.
  - `capabilities = {"arch": <str>, "vcpus": <int>, "memory_mb": <int>, "transports":
    ["gdbstub"], "concurrent_allocation_cap": <int>}`. `vcpus`/`memory_mb` come from
    `getInfo()` (index 2 = cpus, index 1 = memory in MB); `arch` is parsed from the
    `<host><cpu><arch>` element of `getCapabilities()` XML via `xml.etree`. A
    capabilities XML missing that element → `arch = "unknown"` (advertise the host
    anyway; arch is informational in M0, the cap and transport are what admission and
    debug need).
  - `status = "available"` (a reachable connection is available; health flips are the
    reconciler's job, not discovery's).
- **`list_owned()`** iterates `listAllDomains()` and returns an `OwnedInfra`
  `{system_id, domain_name}` for **each domain carrying a kdive `system_id`** in its
  libvirt metadata (namespace below), skipping untagged domains (not ours). The
  metadata read uses
  `domain.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, _KDIVE_METADATA_NS, 0)`; a
  domain with no such metadata raises a libvirt error, caught and treated as "untagged
  → skip". The tag XML shape and namespace are pinned here as the contract #15's
  provisioning must honor:
  - namespace `_KDIVE_METADATA_NS = "https://kdive.dev/libvirt/1"`
  - element `<kdive:system xmlns:kdive="…">{system_id}</kdive:system>` — the text is the
    System uuid.
- **`register_local_libvirt_resource(conn, discovery, *, pool, cost_class)`** is the
  discovery→Postgres bridge, **idempotent** by `(kind, host_uri)`:
  - calls `discovery.list_resources()` (one record), then within one
    `conn.transaction()`: `SELECT id FROM resources WHERE kind=%s AND host_uri=%s FOR
    UPDATE`; if a row exists, `UPDATE` its `capabilities`/`status`/`pool`/`cost_class`;
    else `INSERT` a new `resources` row (uuid + timestamps DB-generated). Returns the
    persisted `Resource`.
  - **M0 single-registrar assumption.** Without a `UNIQUE(kind, host_uri)` constraint,
    two *first-time* concurrent registrations could both insert. M0 registers from one
    startup/operator path, so this is not reachable; the constraint is the M1 hardening
    when multi-registrar lands. Documented, not silently assumed.
  - `pool` here is the resource **pool name** string (e.g. `"local-libvirt"`), not the
    psycopg connection pool — the parameter is named `pool` to match `Resource.pool`.

### `allocation_admission.py` — the capacity-admission core

```python
@dataclass(frozen=True)
class AdmissionOutcome:
    granted: bool
    allocation: Allocation | None       # the granted row, or None on denial
    reason: str | None                  # e.g. "at_capacity" on denial
    cap: int
    in_use: int                         # non-terminal count observed under the lock

_NON_TERMINAL = (AllocationState.REQUESTED, AllocationState.GRANTED,
                 AllocationState.ACTIVE, AllocationState.RELEASING)
_CAP_KEY = "concurrent_allocation_cap"

async def admit(
    conn: AsyncConnection, ctx: RequestContext, *, resource: Resource, project: str
) -> AdmissionOutcome: ...
```

`admit` is the always-yes-but-capacity-checked path:

1. Resolve the cap: `cap = resource.capabilities[_CAP_KEY]`. A missing or non-`int`
   (or `< 0`) cap raises `CategorizedError(CONFIGURATION_ERROR)` — fail closed; a host
   with no cap is a registration bug, not "unlimited".
2. `async with conn.transaction():` then `async with advisory_xact_lock(conn,
   LockScope.RESOURCE, resource.id):` — the count and the insert are one atomic,
   per-host-serialized critical section.
3. `SELECT count(*) FROM allocations WHERE resource_id=%s AND state = ANY(%s)` over
   `_NON_TERMINAL` → `in_use`.
4. If `in_use >= cap`: return `AdmissionOutcome(granted=False, allocation=None,
   reason="at_capacity", cap=cap, in_use=in_use)` — **no row inserted**, and the
   transaction commits with no allocation change (the lock releases).
5. Else: `ALLOCATIONS.insert` an Allocation with `state=GRANTED`, `resource_id`,
   `principal=ctx.principal`, `agent_session=ctx.agent_session`, `project`,
   `capability_scope={}`, `lease_expiry=None`; `audit.record(conn, ctx,
   tool="allocations.request", object_kind="allocations", object_id=alloc.id,
   transition="->granted", args={"resource_id": str(resource.id), "project": project},
   project=project)`; return `AdmissionOutcome(granted=True, allocation=alloc, …)`. The
   insert and the audit row commit together (same transaction).

`admit` raises (it does not build a `ToolResponse`); the tool maps the outcome and any
`CategorizedError` onto the envelope. Admission is pure of MCP — testable with a real
Postgres and a hand-built `RequestContext`/`Resource`.

### `resources.py` — `resources.list` / `.describe`

```python
async def list_resources_tool(pool, ctx, *, kind: str | None) -> list[ToolResponse]: ...
async def describe_resource(pool, ctx, resource_id: str) -> ToolResponse: ...
def register(app: FastMCP, pool: AsyncConnectionPool) -> None: ...
```

- **`resources.list(kind?)`** reads the `resources` rows (optional `kind` filter,
  ordered by `created_at, id` for determinism) and returns one `ToolResponse.success`
  per row: `object_id=str(resource.id)`, `status=resource.status.value`,
  `suggested_next_actions=["resources.describe", "allocations.request"]`, `data=` the
  flat projection `{"kind", "arch", "vcpus", "memory_mb", "transports",
  "concurrent_allocation_cap"}` (all `str`; `transports` comma-joined; missing
  capability keys omitted, never `KeyError`). Resources are shared infra (no `project`
  column), so the tool requires only an authenticated context — **no** `require_project`
  / `require_role`.
- **`resources.describe(resource_id)`** returns the single row's envelope as above plus
  `data["pool"]`, `data["cost_class"]`, `data["host_uri"]`; `suggested_next_actions =
  ["allocations.request"]`. A malformed uuid or absent row →
  `ToolResponse.failure(resource_id, CONFIGURATION_ERROR)`.
- Each row is built defensively in `list` (a row that violates the envelope invariant is
  isolated to a `failure` entry, mirroring `jobs.list`), so one bad resource cannot
  blank the list.

### `allocations.py` — `allocations.request` / `.get` / `.release` / `.list`

```python
async def request_allocation(pool, ctx, *, project, resource_id=None, kind=None) -> ToolResponse: ...
async def get_allocation(pool, ctx, allocation_id) -> ToolResponse: ...
async def release_allocation(pool, ctx, allocation_id) -> ToolResponse: ...
async def list_allocations(pool, ctx, *, project, limit) -> list[ToolResponse]: ...
def register(app: FastMCP, pool: AsyncConnectionPool) -> None: ...
```

- **`allocations.request(project, resource_id?, kind?)`** — the selector is two optional
  fields (`resource_id` wins; else filter by `kind`, default `"local-libvirt"`):
  1. `require_project(ctx, project)` then `require_role(ctx, project, Role.OPERATOR)` —
     creating an allocation is an operator action.
  2. Resolve the resource: explicit `resource_id` → `RESOURCES.get`; else select the
     single resource of `kind` (deterministic lowest `created_at, id` if more than one;
     M0 has one). Zero matches / malformed id → `failure(..., CONFIGURATION_ERROR)`.
  3. `outcome = await admit(conn, ctx, resource=resource, project=project)`.
  4. `granted` → `ToolResponse.success(str(alloc.id), "granted",
     suggested_next_actions=["allocations.get", "allocations.release"],
     data={"resource_id": str(resource.id), "project": project})`.
  5. denied → `ToolResponse.failure(str(resource.id), ErrorCategory.ALLOCATION_DENIED,
     suggested_next_actions=["allocations.list"], data={"reason": outcome.reason,
     "cap": str(outcome.cap), "in_use": str(outcome.in_use)})`. `object_id` is the
     resource id (no allocation was created). The denial is also `log.info`-ed.
  6. `CategorizedError` from `admit` (e.g. cap misconfig) → `failure` with its category.
- **`allocations.get(allocation_id)`** — `require_project` against the **allocation's
  own** project (read the row, then verify `alloc.project in ctx.projects`; a row in a
  project the caller was not granted → `failure(..., CONFIGURATION_ERROR)`, identical to
  "not found" so the tool does not leak the existence of other projects' allocations).
  Returns `success(str(alloc.id), alloc.state.value, …)`.
- **`allocations.release(allocation_id)`** — `require_role(ctx, alloc.project,
  Role.OPERATOR)`; then drive the Allocation to `released`:
  - current `granted` or `active` → `update_state(... RELEASING)`, audit
    `"<old>->releasing"`; then `update_state(... RELEASED)`, audit `"releasing->released"`
    — both inside one `conn.transaction()` so a release is all-or-nothing and writes
    exactly two audit rows.
  - current already `releasing` → drive `→ released` (single transition + audit), so a
    retried release is forward-progressing rather than an error.
  - current terminal (`released`/`failed`) → `failure(..., CONFIGURATION_ERROR,
    data={"current_status": alloc.state.value})` — the agent learns *why* (mirrors
    `jobs.cancel` on a terminal job); no `IllegalTransition` escapes.
  - returns `success(str(alloc.id), "released", suggested_next_actions=[])`.
- **`allocations.list(project, limit=50)`** — `require_project(ctx, project)`; return the
  newest allocations for `project` (capped, `MAX_LIST_LIMIT=200`), each as a
  `success`/`failure` envelope, isolating a bad row per `jobs.list`.

Every handler wraps its body in `bind_context(principal=ctx.principal, …)` (ADR-0014) so
records emitted while serving are attributed, and takes its dependencies (`pool`, `ctx`)
as arguments so it is tested directly, never through MCP transport.

### Domain / locks / envelope changes

- **`state.py`** adds `AllocationState.GRANTED → {ACTIVE, RELEASING, FAILED}` (the
  `RELEASING` edge is new). `tests/domain/test_state.py`'s hand-transcribed `LEGAL`
  table gains the same edge in the same commit, so the legal/illegal parametrization
  stays the spec's executable mirror.
- **`locks.py`** adds `LockScope.RESOURCE = "resource"`; the lock-key derivation is
  unchanged (it already accepts any scope + uuid).
- **`responses.py`** adds:
  - `ToolResponse.success(object_id, status, *, suggested_next_actions=(), refs=None,
    data=None)` — builds a non-failure envelope (`status` must not be a failure status,
    else the existing validator raises, which is the misuse signal).
  - `ToolResponse.failure(object_id, category, *, suggested_next_actions=(), data=None)`
    — builds `status="error"`, `error_category=category.value`.
  Both keep the "category iff failure" invariant centralized.

## Threat model & guarantees

- **Per-host cap holds under concurrency.** The count-and-insert run inside one
  transaction holding `advisory_xact_lock(RESOURCE, resource.id)`, so two concurrent
  `allocations.request` for the same host serialize: the second observes the first's
  inserted `granted` row in its count. Cross-host requests do not contend (different lock
  key). Correctness rests on (a) the lock being held across both the count and the
  insert, and (b) the count's `_NON_TERMINAL` set matching the states that occupy
  capacity — both are pinned by tests.
- **Denials are invisible in Postgres by design.** No `allocations`/`audit_log` row is
  written for a denial; the only record is a structured log line. A caller cannot use a
  burst of denied requests to write rows.
- **`allocations.get` does not leak cross-project existence.** A row in an ungranted
  project returns the same `CONFIGURATION_ERROR` envelope as a missing row.
- **Audit attribution rides on `audit.record`'s own guard** (`project in ctx.projects`,
  #11); every grant and every release transition writes exactly one row, inside the same
  transaction as the state change, so a crash mid-release leaves the Allocation and its
  audit rows consistent.
- **Cap misconfiguration fails closed.** A resource missing/with a bad
  `concurrent_allocation_cap` denies admission via `CONFIGURATION_ERROR`, never "treat as
  unlimited".

## Failure modes & edges (drives the tests)

**discovery** (fake libvirt connection; no DB except for registration)
- `list_resources`: builds the expected `ResourceRecord` with `arch`/`vcpus`/`memory_mb`
  from a fake `getInfo()` + capabilities XML, `transports=["gdbstub"]`, and the injected
  cap; arch absent from the XML → `"unknown"` (host still advertised).
- `list_owned`: a domain tagged with a kdive `system_id` → one `OwnedInfra`; an untagged
  domain (metadata read raises) → skipped; mixed set → only tagged domains returned.
- `from_env`: reads `KDIVE_LIBVIRT_ALLOCATION_CAP`; absent → default `1`; non-int → the
  builder raises `CategorizedError(CONFIGURATION_ERROR)`.
- `register_local_libvirt_resource`: first call inserts one `resources` row with the
  discovered capabilities (incl. the cap) and returns it; a second call with the same
  `host_uri` updates in place (still one row), reflecting changed capabilities —
  idempotent.

**admission** (real Postgres; seeded resource)
- under cap → inserts one `granted` Allocation and exactly one `audit_log` row
  (`transition="->granted"`); `AdmissionOutcome.granted is True`.
- at cap (seed `cap` non-terminal allocations) → `granted is False`,
  `reason="at_capacity"`, **no** new allocation row, **no** audit row.
- the count ignores terminal allocations: a host at cap whose allocations are all
  `released`/`failed` still admits (terminal rows do not occupy capacity).
- a `released`/`requested`/`active`/`releasing` mix is counted exactly per `_NON_TERMINAL`.
- cap missing from `resource.capabilities` / non-int / negative → `CategorizedError`
  (`CONFIGURATION_ERROR`), no row.
- serialization: two `admit` calls racing on one host with `cap=1` yield exactly one
  grant and one denial (proves the lock spans count+insert). (Exercised with two
  connections; if the harness cannot drive true concurrency deterministically, a
  sequential count-then-insert ordering test stands in, plus the lock is asserted to be
  acquired inside the transaction.)

**resources tools** (real Postgres; registered host)
- `resources.list` returns the host with the flat capability projection
  (`kind`/`arch`/`vcpus`/`memory_mb`/`transports`/`concurrent_allocation_cap`) and
  `status="available"`; `kind` filter that matches → the row; that misses → `[]`.
- `resources.describe(id)` adds `pool`/`cost_class`/`host_uri`; malformed uuid / absent →
  `failure(CONFIGURATION_ERROR)`.
- a resource row whose capabilities lack `arch` → projection omits `arch`, no error.

**allocations tools** (real Postgres; registered host; hand-built contexts)
- `request` under cap (operator) → `success` `status="granted"`, real `allocation_id`,
  `data.resource_id`.
- `request` at cap → `failure` `error_category="allocation_denied"`,
  `object_id=resource_id`, `data.reason="at_capacity"`.
- `request` without `operator` (viewer/none) → `AuthorizationError` surfaces as the
  handler's authz mapping (raises; the wire mapping of authz denials is the handler's —
  here the handler lets it raise as #11 established no `ErrorCategory` for authz). *(See
  open question below — pinned before code.)*
- `request` with `project` not granted → `AuthError` (membership), same posture.
- `request` resolving zero resources / bad `resource_id` → `failure(CONFIGURATION_ERROR)`.
- `get` of own-project allocation → `success` with its state; of another project's id →
  `failure(CONFIGURATION_ERROR)` (indistinguishable from not-found); malformed uuid →
  same.
- `release` of a `granted` allocation → `success` `status="released"`; exactly **two**
  audit rows (`granted->releasing`, `releasing->released`); the row ends `released`.
- `release` of an `active` allocation → same `released` result via `active->releasing->released`.
- `release` of an already-`releasing` allocation → drives to `released` (one transition,
  one audit row).
- `release` of a terminal (`released`/`failed`) allocation → `failure(CONFIGURATION_ERROR,
  data.current_status=…)`; no `IllegalTransition` escapes; row unchanged.
- `list(project)` → newest-first allocations for that project; another project's rows
  excluded; `limit` capped at 200; a malformed row isolated to a `failure` entry.

**domain/locks/envelope**
- `can_transition(GRANTED, RELEASING) is True`; `test_state.py` legal/illegal tables
  updated so the parametrized suite covers it and no longer flags it illegal.
- `LockScope.RESOURCE` derives a distinct lock key from `ALLOCATION`/`SYSTEM` for the
  same uuid (no cross-scope collision).
- `ToolResponse.success("x", "granted")` → no `error_category`;
  `ToolResponse.success("x", "error")` raises (misuse: error is a failure status);
  `ToolResponse.failure("x", ErrorCategory.ALLOCATION_DENIED)` → `status="error"`,
  `error_category="allocation_denied"`.

## Open question (resolve before code)

**How does a handler surface an RBAC/membership denial on the wire?** `require_role` /
`require_project` raise `AuthorizationError` / `AuthError`; the M0 taxonomy has **no**
authorization `ErrorCategory` ([ADR-0020](../../adr/0020-rbac-audit-gate-implementation.md),
"do not invent strings"). #14 is the **first** plane handler to call them, so it must
decide the mapping the later handlers inherit. Two options, decided in the spec
`/challenge`:
1. **Let it raise** — the authz exception propagates out of the tool (FastMCP renders a
   non-200), matching #11's stance that authz denials carry no `ErrorCategory` and are
   not `ToolResponse` failures. Simplest, no new category, but the agent gets a transport
   error rather than a structured envelope.
2. **Map to `CONFIGURATION_ERROR`** — wrap the denial in a `failure` envelope with the
   closest existing category. Keeps the uniform envelope, but overloads
   `CONFIGURATION_ERROR` with "you may not".

This spec's default is **option 1** (let it raise) — it honors ADR-0020 literally and
adds nothing; the tests assert the handler raises rather than returns an envelope. The
`/challenge` pass confirms or overrides.

## Testing strategy

Handlers and the admission function are the unit of testing (repo contract): call
`list_resources`/`list_owned`/`register_local_libvirt_resource`, `admit`,
`request_allocation`/`get_allocation`/`release_allocation`/`list_allocations`, and the
`resources.*` handlers **directly** with injected fakes/contexts — never through MCP.

- **Discovery** uses a hand-written `FakeLibvirtConn` (returns canned `getInfo()`,
  `getCapabilities()` XML, and `listAllDomains()` with tagged/untagged fake domains) — no
  libvirt, no `live_vm`. `register_local_libvirt_resource` uses the testcontainers
  Postgres fixtures (`migrated_url`, the `asyncio.run(_run())` idiom) re-exported into
  `tests/providers/conftest.py` (already present) / `tests/domain/` / `tests/mcp/` as
  needed from `tests/db/conftest.py`.
- **Admission** and the **tools** use the same Postgres fixtures; resources/allocations
  are seeded via the existing repositories (or `register_local_libvirt_resource`).
  Contexts are hand-built `RequestContext(...)` with explicit `roles`.
- **Domain/locks/envelope** changes are pure (no DB): extend `tests/domain/test_state.py`,
  `tests/db/test_locks.py`, `tests/mcp/test_responses.py`.
- Tests live in `tests/providers/local_libvirt/` (discovery), `tests/domain/`
  (`test_allocation_admission.py` — admission is a `domain` module), and `tests/mcp/`
  (`test_resources_tools.py`, `test_allocations_tools.py`), mirroring the package layout.
- No new gated/`live_vm` test; nothing here needs libvirt/gdb/drgn at run time.
