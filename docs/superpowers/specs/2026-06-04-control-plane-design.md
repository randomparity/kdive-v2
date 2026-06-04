# Control plane (power + force_crash, gated) — Design

**Issue:** #23 (M0) · **Depends on:** #11 (RBAC/audit/gate — merged), #13 (capability
registry / plane interfaces — merged), #16 (provisioning plane — merged) ·
**Decisions:** [ADR-0027](../../adr/0027-control-plane-power-force-crash.md) (the
decisions this spec realizes), [ADR-0020](../../adr/0020-rbac-audit-gate-implementation.md)
(gate/RBAC/audit), [ADR-0024](../../adr/0024-provisioning-profile-model-shape.md) (profile
shape), [ADR-0025](../../adr/0025-provisioning-plane-libvirt.md) (provisioning plane / the
`Provisioner` port and row-first ordering this spec mirrors),
[ADR-0018](../../adr/0018-job-queue-worker-execution.md) (job handler contract),
[ADR-0019](../../adr/0019-tool-response-envelope.md) (envelope) ·
**Parent spec:** [`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md)
("control/retrieve", the destructive-op gate, "Domain objects in M0 → System / DebugSession")

## Goal

The control plane of the walking skeleton: power a kdive System's libvirt domain on/off/
reset/cycle, and `force_crash` it (panic the guest) behind the destructive-op gate,
driving System `ready → crashed` and any DebugSession `live → detached`.

- `src/kdive/providers/local_libvirt/control.py` — `LocalLibvirtControl`, the `Controller`
  implementation over an **injected** libvirt connection (the same `Connect` factory the
  provisioning/discovery planes use). DB-free: it looks the domain up by name and drives
  libvirt — `power(domain_name, action)` (`on→create`, `off→destroy`, `reset→reset`,
  `cycle→reboot`) and `force_crash(domain_name)` (`injectNMI`). Nothing else.
- `src/kdive/mcp/tools/control.py` — the `control.*` tool surface (`power` / `force_crash`)
  **and** the `power` / `force_crash` job handlers that orchestrate the System (and, for
  force_crash, DebugSession) state machine around the provider, plus `register(app, pool)`
  and `register_handlers(registry, *, control=None)`.

Plus the minimal plumbing the above require:

- `src/kdive/profiles/provisioning.py` — add a `destructive_ops: list[NonEmptyStr]` field
  (default `[]`) to `LibvirtProfile`; the force_crash handler resolves the gate's opt-in
  factor from it.
- `src/kdive/domain/errors.py` — add `ErrorCategory.AUTHORIZATION_DENIED`, the wire
  category a `force_crash` denial maps to.
- `src/kdive/mcp/app.py` — append `control.register` to `_PLANE_REGISTRARS` and
  `control.register_handlers` to `_HANDLER_REGISTRARS`.

This layer sits **above** the repository/locks/RBAC/audit/gate/job primitives and the
profile/provisioning code, and **below** the agent. It owns *powering and crashing a
System's domain* and *the control tool surface*. It does **not** own provisioning/teardown
(#16), build/install/boot (#17/#18), DebugSession *creation* via attach (#20), vmcore
retrieval (#22), or the reconciler loop (#12).

## Non-goals

- **No guest-agent `sysrq-c`.** `force_crash` uses the libvirt NMI (`injectNMI`), which
  needs no in-guest agent and no `<channel>` device (the M0 provisioning XML renders none,
  ADR-0025). The sysrq-c path is deferred to a milestone that provisions the guest agent.
- **No DebugSession *creation*.** #23 only drives existing sessions `→ detached` on a
  crash; *attaching* a session (creating the row, the `attach`/`live` transitions, the
  worker heartbeat) is #20. The detach join-through-`runs` is self-contained and is a
  no-op when no session exists (the common M0 case).
- **No vmcore capture.** A crashed guest's vmcore retrieval is the retrieve plane (#22);
  `force_crash` stops at `crashed` and leaves the `capture_vmcore` job to that plane.
- **No System-lifecycle move on `power`.** A domain restart is not a reprovision; `power`
  acts on the domain and audits the action but moves no System state (ADR-0027 §3). A
  `crashed → ready` recovery edge, if ever modeled, lands with its own ADR.
- **No live libvirt in unit tests.** Every handler/provider test injects a fake controller
  or `Connect`; the real `libvirt.open` adapter and the end-to-end crash are `live_vm`-only.

## Components

### `Controller` port + `LocalLibvirtControl` (`providers/local_libvirt/control.py`)

A handler-facing Protocol mirroring `Provisioner` (ADR-0025), keyed on the libvirt domain
name the provisioning plane stored on `System.domain_name`:

```python
class Controller(Protocol):
    def power(self, domain_name: str, action: PowerAction) -> None: ...
    def force_crash(self, domain_name: str) -> None: ...
```

`PowerAction` is a `StrEnum` (`on`/`off`/`cycle`/`reset`). `LocalLibvirtControl` builds
over the injected `Connect` factory (`from_env` reads `KDIVE_LIBVIRT_URI`, lazy — no
connection at construction). It looks the domain up by name, then:

- `power on` → `domain.create()`; "already running" (`VIR_ERR_OPERATION_INVALID`) is the
  achieved post-state, swallowed (idempotent).
- `power off` → `domain.destroy()`; "not running" (`VIR_ERR_OPERATION_INVALID`) swallowed.
- `power reset` → `domain.reset(0)` (hard reset, no clean shutdown).
- `power cycle` → `domain.reboot(0)` (ACPI reboot).
- `force_crash` → `domain.injectNMI(0)`.

A domain that is absent on lookup (`VIR_ERR_NO_DOMAIN`) is a `CONTROL_FAILURE` (you cannot
power/crash a System whose domain is gone — distinct from teardown's idempotent
absent-is-success). Any other libvirt error is `CONTROL_FAILURE`. The seam suppresses the
single ty `invalid-argument-type` at the `libvirt.open` adapter only (the connect lambda),
never `unresolved-import` (ADR-0025 precedent).

### `control.*` tools + handlers (`mcp/tools/control.py`)

Mirrors `systems.py`'s structure (the `_authorizing`/`_ctx_from_job`/`_audit_transition`
helpers are re-derived locally; no cross-tool import).

**`control.power(system_id, action)`** (synchronous admission, async execution):
1. Parse `system_id` (malformed → `configuration_error`) and `action` (unknown →
   `configuration_error`).
2. Load the System; not-found or cross-project → `configuration_error` (not-found-shaped).
3. `require_role(ctx, system.project, Role.OPERATOR)` (raises `AuthorizationError`).
4. Refuse on a System with no live domain: a `defined`/`provisioning`/terminal System has
   nothing to power (`configuration_error` carrying `current_status`). Only `ready`/
   `crashed` Systems (have a started domain) admit a power op.
5. Enqueue a `POWER` job, `dedup_key=f"{system_id}:power:{action}"`, payload
   `{system_id, action}`. Return the job-handle envelope (System id in `data`).

**`control.power` handler** (`power` job): under the per-System lock, load the System
(missing → `infrastructure_failure`), read `domain_name` (or the deterministic name),
call `controller.power`, and audit `transition=f"power:{action}"`. No System state change
(ADR-0027 §3).

**`control.force_crash(system_id)`** (synchronous admission, async execution):
1. Parse `system_id` (malformed → `configuration_error`).
2. Load the System + its Allocation (for the gate's capability scope and project).
   Not-found / cross-project → `configuration_error`.
3. Resolve the profile opt-in: `"force_crash" in
   ProvisioningProfile.parse(system.provisioning_profile).provider.local_libvirt.destructive_ops`.
4. `assert_destructive_allowed(ctx, allocation, DestructiveOp("force_crash", opt_in))`. On
   `DestructiveOpDenied`: audit `transition="force_crash:denied"` (`args` carrying
   `missing`) and return `ToolResponse.failure(system_id, AUTHORIZATION_DENIED)`.
5. Refuse unless the System is `ready` (only a ready System can crash; `current_status`
   in `data` otherwise → `configuration_error`).
6. Enqueue a `FORCE_CRASH` job, `dedup_key=f"{system_id}:force_crash"`, payload
   `{system_id}`. Return the job-handle envelope.

**`control.force_crash` handler** (`force_crash` job): under the per-System lock —
1. Load the System (missing → `infrastructure_failure`).
2. If already `crashed`/terminal: idempotent — re-attempt the NMI where the System is
   still `crashed` (the domain may still be up), make no illegal transition, and still
   detach sessions; a terminal System returns without crashing.
3. Call `controller.force_crash(domain_name)`.
4. In one transaction under the lock: drive System `ready → crashed`, audit
   `transition="ready->crashed"`, then `UPDATE debug_sessions SET state='detached' WHERE
   state IN ('attach','live') AND run_id IN (SELECT id FROM runs WHERE system_id=…)` and
   audit one `live->detached` (or `*->detached`) row per detached session.

The gate is **only** on `force_crash`; `power` is `operator`-authorized and ungated
(ADR-0027 §3).

### Plumbing

- `LibvirtProfile.destructive_ops: list[NonEmptyStr] = []` — opted-in op kinds. Additive,
  default-empty, so existing profiles validate unchanged and cannot force_crash.
- `ErrorCategory.AUTHORIZATION_DENIED = "authorization_denied"` — the denial wire string.
- `JobKind.FORCE_CRASH` / `JobKind.POWER` already exist (domain/models.py).
- `app.py`: append the two registrars.

## Sequence (force_crash, happy path)

```
agent → control.force_crash(system_id)
  load System + Allocation; resolve opt-in from profile
  gate: scope ∧ admin ∧ opt-in        ── denied → audit force_crash:denied, AUTHORIZATION_DENIED
  System.state == ready?              ── no → configuration_error(current_status)
  enqueue FORCE_CRASH (dedup system_id:force_crash) → {job_id}
worker → force_crash handler
  lock(System)
  controller.force_crash(domain_name)  (injectNMI)
  txn: System ready→crashed + audit; debug_sessions live→detached + audit
```

## Failure contract

| Condition | Result |
|-----------|--------|
| malformed `system_id` / unknown `action` | `configuration_error` |
| System not found / cross-project | `configuration_error` (not-found-shaped) |
| `power` on a System with no started domain | `configuration_error` (`current_status`) |
| `force_crash` on a non-`ready` System | `configuration_error` (`current_status`) |
| `force_crash` missing any gate check | `authorization_denied` + audited `force_crash:denied` |
| `power`/`force_crash` without `operator`/`admin` role | raises (RBAC), per the gate split |
| provider libvirt error (incl. absent domain) | handler dead-letters `control_failure` |
| handler target System gone | `infrastructure_failure` |

## Redaction

No guest output crosses this plane: `force_crash`/`power` carry only a `system_id`, an
`action`, and (on denial) the gate's `missing` check names — all caller-supplied or
policy-internal, none guest-derived. The job payload and audit `args` carry the same;
`args_digest` one-ways them regardless. No response or persisted row carries guest memory,
the vmcore, or secret material (the vmcore is the retrieve plane's, #22).

## Testing (handlers/providers as the unit, injected fakes)

- **Gate refusal (acceptance):** `force_crash` refused with each of {no scope, no admin,
  no opt-in}, and all three absent → `authorization_denied` + a `force_crash:denied` audit
  row; the provider is never called and the System stays `ready`.
- **force_crash happy path:** handler drives `ready→crashed`, calls `injectNMI` once,
  detaches a seeded `live` session (and an `attach` one) to `detached`, audits both
  transitions; a System with no session detaches nothing (no-op).
- **force_crash idempotency:** a re-run on an already-`crashed` System makes no illegal
  transition; a terminal System returns without crashing.
- **power:** each action maps to the right libvirt call (fake records it), audits
  `power:{action}`, moves no System state; `power` on a `defined`/terminal System →
  `configuration_error`; idempotent `on`/`off` swallow the achieved-post-state error.
- **RBAC:** `power` without `operator` and `force_crash` without the gate raise/deny.
- **edges:** malformed uuid, unknown action, missing System (handler), absent domain
  (`control_failure`), cross-project not-found.
- **registration:** `register_handlers` binds both `POWER` and `FORCE_CRASH`.
- **profile:** `destructive_ops` parses, defaults `[]`, rejects non-list / blank entries.
- **`live_vm` (gated, never un-gated):** real `injectNMI` panics a kdump guest and the
  System+DebugSession transition correctly end-to-end.

## Exit criteria

1. `control.power` and `control.force_crash` tools + handlers ship, registered via the two
   `app.py` seams.
2. `force_crash` passes the three-check gate; a denial is `authorization_denied` + audited.
3. `force_crash` drives System `ready→crashed` and DebugSession `live→detached`.
4. Guardrails green: `ruff check`/`format`, `ty check src`, `pytest -q`; `live_vm` gated.
