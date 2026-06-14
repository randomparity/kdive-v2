# ADR 0105 — `kdivectl tool call` reaches mutating/destructive tools by explicit opt-in (#368)

- **Status:** Proposed
- **Date:** 2026-06-13
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0089](0089-operator-cli-mcp-client.md) (the
  operator CLI as an authenticated MCP client; this ADR relaxes its decision 2a — the
  read-only-by-policy passthrough — to a *tiered* opt-in, keeping read-only the default),
  [ADR-0020](0020-rbac-audit-gate-implementation.md) / [ADR-0006](0006-oidc-rbac-attribution.md)
  (the server-side RBAC, audit, and destructive-op gate that remain the real boundary),
  [ADR-0047](0047-tool-annotations.md) (the `read_only` / `mutating` / `destructive`
  `ToolAnnotations` the gate classifies on).
- **Issue:** [#368](https://github.com/randomparity/kdive/issues/368) (MCP coverage campaign
  finding F1).

## Context

ADR-0089 decision 2a made the generic `kdivectl tool call <name>` passthrough **read-only by
policy, fail-closed**: `assert_read_only` admits a tool only when its MCP `readOnlyHint` is
exactly `True`, and refuses everything mutating, unannotated, or unknown (exit 3). Every state
change was routed through a curated break-glass verb; there was deliberately no generic escape
hatch.

The MCP tool coverage campaign (`docs/reports/mcp-coverage-campaign-2026-06-13.md`, finding F1)
showed the cost of that choice: of 91 registered tools, only a handful of curated verbs and the
`images` verbs can mutate from the shipped CLI. An agent or operator restricted to `kdivectl`
**cannot reach the bulk of the mutating/destructive tool surface at all** — not to administer,
not to drive a lifecycle, not to reproduce a campaign step. The curated-verb surface is small
and grows one hand-written verb at a time; the passthrough is the only mechanism that scales to
the whole tool census, and it is sealed shut against mutation.

The constraint to preserve is precise: **the CLI is a pure MCP client.** The server-side
destructive-op gate (ADR-0006/0020) — capability scope + RBAC role + profile opt-in, deny by
default — is the real authorization boundary and applies to every call regardless of what the
client does. A `tool call` to a destructive tool is exactly the same authenticated MCP call an
agent with a raw client would make; it cannot bypass server authz, break-glass routing, or audit
attribution (the operator-cli `actor` of ADR-0089 decision 5 is recorded by the server middleware
on every call, including these). So the read-only gate is **purely a client-side policy/UX guard**,
not a security control. The decision is only: should that client-side guard keep blocking mutation
outright, or relax to an explicit, auditable opt-in?

## Decision

**Relax the `tool call` gate from "read-only only" to a three-tier classifier with an explicit
per-tier opt-in. Read-only stays the zero-flag default; mutation requires a deliberate flag.**

1. **Classify, don't just admit/refuse.** Replace the binary `assert_read_only` with
   `classify_tool(tool) -> ToolTier`, deriving the tier from the same `ToolAnnotations` the server
   registers (ADR-0047):
   - `readOnlyHint is True` → `READ_ONLY`
   - `readOnlyHint is False and destructiveHint is True` → `DESTRUCTIVE`
   - `readOnlyHint is False and destructiveHint is not True` → `MUTATING`
   - anything else (missing annotations, missing/`None` `readOnlyHint`, a truthy-but-not-`True`
     hint, an absent tool) → `UNKNOWN`

   `UNKNOWN` remains **fail-closed and unreachable by any flag** — an unannotated or unresolvable
   tool is never callable through the passthrough. This preserves ADR-0089's "nothing silently
   slips through": only a tool the server has *positively* annotated mutating/destructive becomes
   reachable, and only with the matching opt-in.

2. **Two opt-in flags on `tool call`, deny-by-default, monotonic:**
   - no flag → only `READ_ONLY` admitted (unchanged default behavior).
   - `--allow-mutating` → additionally admits `MUTATING`.
   - `--allow-destructive` → additionally admits `MUTATING` and `DESTRUCTIVE`
     (`--allow-destructive` implies `--allow-mutating`; a destructive call needs only the one
     stronger flag, not both).

   The flag names the *maximum* tier the caller has authorized for this single invocation. A
   `READ_ONLY` call still works with either flag present (the flag widens, never narrows). A
   refused call still exits 3 (`_NOT_READ_ONLY_EXIT`, renamed `_TIER_NOT_ALLOWED_EXIT`) with a
   message that names the tool, its tier, and the flag that would admit it.

3. **A destructive call also requires interactive confirmation, suppressible only for
   automation.** `--allow-destructive` alone is necessary but not sufficient when stdin is a TTY:
   the CLI prints the tool name and tier and prompts for a typed `yes` before dispatching, mirroring
   the curated `teardown --force` break-glass acknowledgement. For non-interactive use (agents, CI,
   pipes) the prompt is unanswerable, so a `--yes` flag (or a non-TTY stdin with `--yes`) skips it;
   without `--yes` and without a TTY the call is refused (exit 3) rather than hanging on a read that
   never returns. `--yes` has no effect on the tier flags — it only discharges the destructive
   confirmation. Mutating (non-destructive) calls need no confirmation, only `--allow-mutating`.

4. **The server contract is unchanged.** `tool call` still lists the server's tools, classifies the
   requested one from the *server's* live annotations (not a client-side allowlist), and on admission
   makes the identical `client.call_tool` it makes today. No new tool, no annotation change, no authz
   change, no audit change. The token-`exp` preflight that guards the curated destructive verbs
   (ADR-0089) is extended to `DESTRUCTIVE` passthrough calls so a near-expired token is refused up
   front rather than risking a mid-operation 401 — consistent with "destructive verbs are single-call
   and re-runnable."

## Consequences

- **The whole annotated tool surface becomes drivable from the shipped CLI**, closing F1: an
  operator/agent can reach any mutating tool the server authorizes them for, with an explicit,
  visible acknowledgement of the blast radius they are accepting. Curated verbs remain the
  ergonomic, argument-validated path for the common operations; the passthrough is the
  scales-to-everything escape hatch.
- **The client-side guard is now a graduated speed-bump, not a wall.** This is deliberate and
  safe *because* the guard was never the security boundary — server-side authz is. The opt-in's
  job is to make an operator type a flag (and, for destructive, confirm) before a mutation, so a
  fat-fingered read command cannot turn into a teardown. The named worst case — an operator who
  passes `--allow-destructive --yes` on the wrong id — is still fully bounded by the server's
  destructive-op gate and recorded in the audit log as an operator-cli action.
- **ADR-0089's "read-only by policy" invariant (decision 2a, 4) is narrowed, not broken.** The
  passthrough is still read-only *by default*; it is no longer read-only *only*. Decision 4's
  claim that "there is no `tool call` route to a mutating tool" no longer holds — but the
  property that mattered (no *silent* or *default* mutation, every mutation server-authz-gated and
  audited) holds. The curated break-glass verbs remain the sole *zero-ceremony* mutation path; the
  passthrough mutation path always carries an explicit per-invocation opt-in.
- **The annotation guard tests stay meaningful.** `UNKNOWN` staying unreachable keeps the
  incentive to annotate tools correctly. The existing guard that every curated read verb targets a
  read-only tool, and every mutating verb a non-read-only tool, is untouched.
- **Defaults do not regress.** Existing scripts that run `kdivectl tool call somereadtool`
  unchanged still work and still refuse mutation without a flag.

## Considered & rejected

- **Keep it read-only; grow curated verbs to cover everything.** Rejected: 91 tools (growing) is
  an unbounded hand-maintenance burden, and the campaign needs to drive arbitrary tools, including
  ones with no curated verb. The curated verbs stay for ergonomics but cannot be the only mutation
  path.
- **A single `--force`/`--allow-mutating` flag with no destructive tier.** Rejected: it collapses
  a cordon (mutating, reversible) and a force-crash/teardown (destructive, data-affecting) into one
  acknowledgement. The server already distinguishes `mutating()` from `destructive()`; the client
  opt-in should honor that gradient so the strongest acknowledgement is reserved for the highest
  blast radius.
- **A separate `tool call-mutating` subcommand.** Rejected: it forks the passthrough, duplicates
  argument parsing and the list/classify/call flow, and still needs a destructive distinction
  inside it. A flag on the one `tool call` is simpler and keeps a single code path.
- **Drop the client-side gate entirely (rely on server authz alone).** Rejected: the gate is a
  cheap, valuable UX guard against accidental mutation; removing it would let a typo in a tool name
  silently dispatch a destructive call. The cost of keeping it is one flag.
- **Make `--yes` (skip-confirm) also widen the tier.** Rejected: conflating "I authorize tier X"
  with "don't prompt me" makes the dangerous case (destructive) the easiest to trigger with one
  flag. Tier authorization and confirmation suppression stay orthogonal.
