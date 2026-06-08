# ADR 0045 — Spine driver: out-of-band capability grant + phase-failure naming contract

- **Status:** Proposed
- **Date:** 2026-06-04
- **Deciders:** kdive maintainers
- **Refines:** [ADR-0042](0042-live-stack-e2e-mcp-http.md) §4 (phase-structured spine) and
  the three-check destructive gate ([ADR-0028](0028-control-plane-power-force-crash.md),
  [ADR-0020](0020-rbac-audit-gate-implementation.md)).
- **Spec:** [`../superpowers/specs/2026-06-04-spine-driver-design.md`](../superpowers/specs/2026-06-04-spine-driver-design.md)
  (sub-issue D / [#100](https://github.com/randomparity/kdive/issues/100)).

## Context

The live-stack spine driver (sub-issue D) drives `allocate → … → release → report` over the
MCP HTTP transport and must pass the `crash` phase, which calls `control.force_crash`. That
tool is behind the three-check destructive gate
(`kdive.security.authz.gate.assert_destructive_allowed`): it requires the **admin** role on the
allocation's project, the controlling allocation's **`capability_scope.destructive_ops`** to
grant `force_crash`, **and** the provisioning profile to opt `force_crash` in via
`provider.local-libvirt.destructive_ops`.

Two of the three factors are reachable from the wire surface the driver uses: the admin role
comes from the `admin` OIDC token (sub-issue A's `mint_token`), and the profile opt-in is a
field of the `systems.provision` `profile` dict. **The capability scope is not.** The wire
`allocations.request` tool always grants an **empty** scope —
`allocation_admission._grant` constructs the `Allocation` with `capability_scope={}`, and no
shipped tool mutates it afterward. Granting a destructive capability is a privileged platform
action deliberately kept off the per-project operator surface (a project operator must not be
able to self-grant `force_crash`); ADR-0042 forbids adding product code in this epic. So the
driver needs a way to establish the scope that does not invent a new tool and does not weaken
the gate.

Separately, ADR-0042 §4 mandates that a spine failure **names its phase** ("a boot failure
reports `boot`"), but leaves the concrete mechanism to this sub-issue.

## Decision

**1. The driver grants the destructive capability scope out of band, via a privileged DB
update, before the `crash` phase.** After the `allocate` phase returns the `allocation_id`,
the driver issues a single `UPDATE allocations SET capability_scope = … WHERE id =
<allocation_id>` against the **same Postgres the stack uses** (`KDIVE_DATABASE_URL`), setting
`{"destructive_ops": ["force_crash"]}`. This mirrors exactly what
`seed_granted_allocation(capability_scope=…)` does for the in-process gate tests: it stands in
for the privileged platform/admin action that would, in a real deployment, grant a destructive
capability to an allocation. It is **setup the driver performs deterministically up front**,
not behaviour the `crash` phase discovers (the spec's "established up front" requirement). The
gate is **not** weakened — all three independent checks still run against real data over the
wire at `crash`; the driver only supplies the one factor no wire tool exposes.

**2. Each phase runs inside a `phase(name)` async context manager that re-raises any failure as
`SpinePhaseError(phase=name)`.** A phase that raises, or whose tool envelope returns
`status in {"error","failed"}`, is converted to a `SpinePhaseError` chaining the original
(`raise … from exc`) and carrying any `error_category`. The test body is a linear sequence of
`async with phase("provision"): …` blocks, so both the failure message and the chained
traceback name the failing phase.

The async job-kind phases drive jobs to a terminal state with a bounded loop that distinguishes
**three** outcomes of `jobs.wait` (whose server side, `wait_job`, returns the job envelope on
any terminal state **or** when its clamped 300s cap elapses): `succeeded` proceeds;
`failed`/`canceled` raises `SpinePhaseError(phase, error_category)` (a failed job is never read
as not-yet-done); a non-terminal return re-issues `jobs.wait` until a per-phase
`_DRAIN_DEADLINE_S` (set above the 300s cap) expires, then raises a timeout `SpinePhaseError`.
So a stalled worker fails the phase by name with a timeout rather than hanging.

The RBAC-negative assertions distinguish the two wire mechanisms a denial can take: a
`require_role` denial **raises** (no authz `ErrorCategory`), which fastmcp surfaces as a tool
error (`CallToolResult.is_error`) — the harness's `LiveStackClient.call_tool` is extended
additively to raise a typed `LiveStackToolError` on that surface, leaving envelope parsing
intact; a `force_crash` gate denial **returns** a `ToolResponse.failure(..., AUTHORIZATION_DENIED)`
envelope (it audits first), asserted on `error_category`.

## Consequences

- The `crash` phase exercises the **real** three-check gate end to end over the wire (admin
  token + real `capability_scope` row + real profile opt-in) — the gate's positive path is
  proven on the live stack, not simulated.
- The driver takes a dependency on `KDIVE_DATABASE_URL` (already required for the #2 audit and
  #5 teardown assertions), so the preflight already gates on it; no new operator obligation.
- The out-of-band grant is a **test-side privileged action**. If a future release adds a real
  platform tool to grant destructive capabilities (e.g. `allocations.grant_capability`, a
  platform-RBAC surface), the driver should switch to it so the grant is also exercised over
  the wire. Until then the DB update is the honest stand-in and is documented as such.
- Phase naming is structural: every new phase is one `async with phase(...)` block, so the
  "names its phase" guarantee cannot rot as phases are added.

## Alternatives considered

- **Add an `allocations.grant_capability` (or `request`-with-scope) tool so the grant is on the
  wire.** Cleanest long-term, and would exercise the grant over HTTP. Rejected for this epic:
  ADR-0042 forbids new product code here, and granting destructive capabilities is a
  platform-scoped authorization decision that belongs to the platform-RBAC tier
  ([ADR-0043](0043-platform-scoped-rbac-tier.md)), not bolted onto the per-project operator
  surface under deadline. Recorded as the migration target above.
- **Mint an `admin` token and have `force_crash` skip the capability-scope check for admins.**
  Collapses two independent gate factors into one and defeats the gate's defence-in-depth (the
  scope exists precisely so an admin token alone cannot crash an allocation that never opted
  in). Rejected — it would change product security semantics to satisfy a test.
- **Seed the allocation entirely out of band (skip the `allocate` phase tool call).** Loses the
  `allocate` phase's wire coverage and its `->granted` audit/ledger row (#2/accounting).
  Rejected: the driver must drive `allocate` over the wire and *then* augment only the one
  factor no tool exposes.
- **Return a plain assertion error per phase instead of a typed `SpinePhaseError`.** Simpler,
  but a bare assertion does not reliably carry the phase name into the failure summary, and an
  early-return `error` envelope would pass silently. A typed error that wraps both raises and
  failure envelopes makes the ADR-0042 §4 guarantee enforceable.
