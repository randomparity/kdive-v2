# ADR 0027 — Control plane: power + force_crash on local libvirt (M0)

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-04
- **Deciders:** kdive maintainers
- **Refines:** [ADR-0020](0020-rbac-audit-gate-implementation.md) (destructive-op gate),
  [ADR-0024](0024-provisioning-profile-model-shape.md) (provisioning-profile shape),
  [ADR-0025](0025-provisioning-plane-libvirt.md) (provisioning plane on libvirt)

## Context

Issue #23 adds the M0 control plane: `power(on|off|cycle|reset)` and `force_crash`
for the local-libvirt provider. `force_crash` is the first **destructive** op that
reaches the three-check gate ([ADR-0020](0020-rbac-audit-gate-implementation.md)) from
a real handler, and the first op that drives two object lifecycles at once: System
`ready → crashed` and DebugSession `live → detached`.

ADR-0020 pinned the gate as pure policy over `(ctx, allocation, op)` and left four
shapes to the first destructive handler: where `op.profile_opt_in` is sourced, what
wire `ErrorCategory` a denial maps to, the control provider port, and how the
DebugSession transition is driven while the debug plane (#20, attach) is unmerged.
ADR-0025 fixed the provisioning port (`Provisioner`) and the row-first ordering this
plane reuses. This ADR pins the control-plane shapes so the diff compiles against a
stable surface.

## Decision

1. **A `control` provider port, mirroring `Provisioner`.** A handler-facing
   `Controller` Protocol (`src/kdive/providers/local_libvirt/control.py`) exposes
   `power(domain_name, action)` and `force_crash(domain_name)`, keyed on the libvirt
   **domain name** (the provisioning plane already stores `System.domain_name`).
   `LocalLibvirtControl` satisfies it over the same injected `Connect` factory the
   provisioning/discovery planes use, so unit tests inject a fake and never touch a
   libvirt host (the real `libvirt.open` adapter is `live_vm`-only). Power maps to the
   libvirt domain calls: `on → create`, `off → destroy`, `reset → reset`,
   `cycle → reboot`. `force_crash` panics the guest via the QEMU NMI
   (`domain.injectNMI()`), the in-process analogue of sysrq-c that needs no guest
   agent; the sysrq-c path (guest agent) is deferred.

2. **`force_crash` opt-in rides on the provisioning profile's libvirt section.** The
   gate's third factor (`op.profile_opt_in`, ADR-0020 §3) is resolved by the handler
   from a new `destructive_ops` field on `LibvirtProfile`
   ([ADR-0024](0024-provisioning-profile-model-shape.md)): an optionally-empty list of
   opted-in op kinds (`["force_crash"]`). The handler resolves
   `op.profile_opt_in = "force_crash" in profile.provider.local_libvirt.destructive_ops`
   and passes it on `op`. This keeps profile-schema knowledge in the handler and the
   gate pure, exactly as ADR-0020 decided. The field defaults to `[]`
   (deny-by-default), so an unmodified profile cannot force_crash.

3. **`power` is not gated; `force_crash` is.** The destructive-op gate guards
   `force_crash` only. `power` actions are reversible operational controls (the
   walking-skeleton's lifecycle moves), authorized at `operator` like
   `systems.teardown`, not at `admin`. `power off`/`reset`/`cycle` are state-machine
   no-transition ops in M0 (they do not move the System lifecycle — a libvirt domain
   restart does not re-provision); they act on the domain and audit the action.

4. **A denial maps to a new `authorization_denied` `ErrorCategory`.** ADR-0020 §
   Consequences flagged that the first handler returning a denial as a `ToolResponse`
   forces this taxonomy decision. `force_crash` is that handler: a `DestructiveOpDenied`
   caught at the tool boundary becomes `ToolResponse.failure(…,
   ErrorCategory.AUTHORIZATION_DENIED)` and is audited with
   `transition="force_crash:denied"` and `args` carrying `missing`, per ADR-0020.

5. **The force_crash handler drives both lifecycles; the DebugSession transition is a
   self-contained seam.** The `force_crash` job handler, under the per-System advisory
   lock, drives System `ready → crashed`, then detaches every non-terminal DebugSession
   of that System (`attach`/`live → detached`) found by joining `debug_sessions` to
   `runs` on `system_id`. This is the System-side transition #23 owns; it does not
   depend on #20 (which owns *creating* sessions via attach). When no session exists
   (the common M0 case — no debug plane yet), the detach step is a no-op. The detach is
   committed in the same transaction as the System transition and its audit row.

6. **`dedup_key` and idempotency.** `force_crash` uses `dedup_key=f"{system_id}:force_crash"`;
   `power` uses `dedup_key=f"{system_id}:power:{action}"` (the `op[:action]` the issue
   names). The handlers are idempotent: a re-run whose System is already `crashed`
   (force_crash) or whose domain is already in the target power state re-attempts the
   provider call where it is idempotent and makes no illegal transition.

## Consequences

- The control plane reuses the provisioning plane's seams verbatim: row-first
  ordering, the `Connect` factory, the per-System advisory lock, the
  `_ctx_from_job`/`_audit_transition` job-attribution helpers, and the
  `register`/`register_handlers` registrar pair appended to `app.py`'s two tuples.
- `LibvirtProfile` gains a `destructive_ops` field. This changes the provisioning
  profile model; the JSON-schema snapshot (if any) is regenerated. The field is
  additive and defaults to `[]`, so existing profiles validate unchanged.
- `ErrorCategory.AUTHORIZATION_DENIED` is added — its first real producer is the
  `force_crash` denial path, so it is not a phantom value.
- The DebugSession detach is driven by the control plane now and by the reconciler's
  dead-session sweep already; when #20 ships attach + a worker heartbeat, the two
  remain consistent (both only move `attach`/`live → detached`, never backward).
- Power ops do not move the System lifecycle in M0. If a later milestone models
  `crashed → ready` on a successful power-cycle recovery, that transition is added to
  `state.py` then, with its own ADR; M0 keeps power orthogonal to the lifecycle.

## Alternatives considered

- **Gate `power` too (require `admin`/opt-in).** Rejected: `power off`/`reset` are
  reversible operational controls, not data-destroying ops; gating them at `admin`
  would block the `operator` tier that legitimately cycles a stuck System and has no
  acceptance backing in the issue (only `force_crash` has the gate-refusal acceptance).
  `power off` is recoverable (`power on`); `force_crash` destroys guest run state and
  is the irreversible op the gate exists for.

- **Resolve `profile_opt_in` from a separate per-allocation flag, not the profile.**
  Rejected: ADR-0020 §3 names the source as "the System's provisioning profile/flag",
  and the System already carries its frozen `provisioning_profile`. A second source
  (an allocation column) would duplicate the capability-scope check the gate already
  does and split opt-in across two records. Keeping it on the profile section the
  provider already owns is one source of truth.

- **A QEMU-monitor `nmi` over a guest-agent `sysrq-c` for the crash mechanism.**
  Chosen `injectNMI` (the libvirt NMI binding) over a guest-agent sysrq write:
  `injectNMI` needs no in-guest agent and no `<channel>` device in the domain XML
  (which the M0 provisioning XML does not render, ADR-0025). The sysrq-c path is
  deferred to a milestone that provisions the guest agent.

- **Drive the DebugSession transition only in #20, leaving force_crash System-only
  now.** Rejected: the issue's acceptance is "force_crash … transitions System+
  DebugSession correctly", and the join-through-runs detach is self-contained and
  testable today (seed a `live` session, assert it goes `detached`). Deferring it would
  leave the acceptance unmet and a `live` session stranded after a crash until the
  reconciler's stale sweep. The seam is clean: #20 owns *creating* sessions; #23 owns
  *detaching* them on crash.

- **A no-transition `power` that still audits vs. a silent power op.** Chose to audit
  every power action (`transition=f"power:{action}"`) even though it moves no lifecycle
  state, so a reversible control still leaves an append-only trail, matching the
  "every operator action is audited" intent without inventing a lifecycle edge.
