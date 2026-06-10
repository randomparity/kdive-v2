# M2.2 — Admin CLI (`kdivectl`) — Design

**Status:** accepted · **Date:** 2026-06-10 · **Milestone:** M2.2 — Admin CLI (`kdivectl`) (GitHub milestone to be created at plan time) · **Owner:** David Christensen

Companion ADR: [ADR-0089](../../adr/0089-operator-cli-mcp-client.md).
Band context: [`2026-06-10-m2x-productionization-band-design.md`](2026-06-10-m2x-productionization-band-design.md).

## Context

The M2.x productionization band makes kdive *operable by someone other than its
author*. M2.1 delivered the published image + config surface. M2.2 delivers the
supported administrative surface that replaces the ad-hoc `psql`/`mc` poking the
author used to stand the service up: a CLI, **`kdivectl`**, for operators
(`platform_admin` / `platform_operator`) — not for agents.

The band design fixes the *what* (two scopes: read-only inspection first, then
mutating/destructive administration; auth is the same OIDC + RBAC boundary as the
MCP surface, not a bypass; mutations route through the M1.3 break-glass path). This
spec settles the *how* the band deliberately left open — "the HTTP API or an
in-process path" — and decomposes the milestone.

### Two facts from the codebase that decide the architecture

1. **There is no HTTP REST API.** The only transport is the FastMCP streamable-HTTP
   server (`python -m kdive server`), with `JWTVerifier` bearer-token auth
   (`mcp/auth.py`) and a `DenialAuditMiddleware` (`mcp/middleware.py`). The
   operator-facing **ops MCP tools already exist** (`inventory`, `queue`,
   `reconcile`, `resources`, `tuning`, `breakglass`, `audit`) and are platform-role
   gated.
2. **kdive issues no tokens — it is a pure verifier.** `build_verifier()` validates
   `iss`/`aud`/signature against the IdP's JWKS and reads `platform_roles` straight
   from token claims (`authz/context.py`). There is no user table and no "create
   first admin" inside kdive. Identity and role assignment live in the external IdP
   (the `navikt/mock-oauth2-server` in dev/CI; a real IdP in prod).

Fact 1 makes "wrap the existing MCP surface in a CLI" the lowest-cost *correct*
option: it reuses the one place auth + audit + RBAC are already solved. Fact 2
dissolves the bootstrap chicken-and-egg: the first admin is provisioned in the IdP,
not in kdive, so the CLI needs to *acquire and present* a token, never to mint a
privileged kdive credential.

## Decision

`kdivectl` is an **MCP client** to the existing FastMCP server. It reaches the
service layer only through the JWT/HTTP MCP surface the agents use, inheriting the
`JWTVerifier` and the RBAC gate unchanged. The one bounded server-side change it
requires is in audit attribution (see Boundary invariant): the middleware learns to
record the caller's client identity so an operator-CLI call is distinguishable from an
agent call. kdive stays a pure verifier; `kdivectl` holds only the bearer token the
IdP minted for the human running it.

### Three layers

1. **Generic client core** (`kdive.cli`). Resolves the server URL from the config
   registry, attaches the bearer token (from `kdivectl login`'s cache, or
   `KDIVE_TOKEN` / a credentials file), maps MCP tool-errors to CLI exit codes, and
   exposes a raw `kdivectl tool call <name> --json '{…}'` passthrough so the **MCP
   surface is reachable by humans** — the broad "features humans currently reach
   through GUIs" win, at near-zero cost once the substrate exists. **The passthrough is
   read-only, fail-closed:** it allows only tools positively annotated `mutating: false`
   in their registration metadata and refuses everything else (mutating, unannotated, or
   unknown). The annotation is added incrementally to the read-only tools the passthrough
   should expose — not a codebase-wide retrofit — so a mutating tool can never slip
   through `tool call`, and every state change goes through a curated break-glass verb.
   (Server-side per-tool authz still applies regardless; the client-side refusal is a
   policy/UX guard, not the security boundary.)

2. **Curated operator verbs**, layered on the core, operator-shaped (human table by
   default, `--json` on every command). Read-only verbs land first (low risk).
   Mutating verbs route through the **M1.3 break-glass path**
   (`mcp/tools/ops/breakglass.py`, `services/allocation/release.py`) — **not** the
   per-allocation iteration gate (`security/authz/gate.py`), which is
   allocation/profile-scoped for an agent iterating, not an operator administering.

3. **`kdivectl login`.** Drives the mock-OIDC authorization-code flow with a principal
   parameterized on the **platform-role axis the CLI acts on** —
   `--platform-role platform_admin|platform_operator` (or none) — *not* the
   project-scoped `admin|operator|viewer` `Role` triad, which is a different axis
   (`security/authz/rbac.py`: project `Role` vs `platform_roles`). The operator surface
   and every mutating verb gate on `platform_roles`, so this is the axis the boundary
   test must exercise. It then caches the token. Prod brings its own token via
   `KDIVE_TOKEN` or a credentials file. A full real-IdP interactive flow (PKCE /
   device-code) is **deferred** — the boundary exit test needs only the mock's
   parameterized-claims capability.

### Boundary invariant (the milestone's spine)

Every `kdivectl` action is an authenticated MCP call, attributed in the audit log
alongside the existing `(principal, agent_session)` tuple with the **caller's client
identity**. Mechanism: `kdivectl` authenticates under a distinct OIDC `client_id`
(surfaced in `azp`/`client_id`), and the audit middleware records it from a closed
actor-map (`kdivectl`-client → `operator-cli`; recognized agent client + `agent_session`
→ `agent`; **`actor=unknown`** for anything matching neither, never defaulting to
`agent`). **Prod prerequisite:** operators register a dedicated `kdivectl` OIDC client
distinct from the agents' client; the dev mock issues it on demand. An under-privileged
principal (no `platform_role`) driving a mutating verb is **denied and audited** —
proven by test, not assumed. The whole `kdive.cli.*` package imports no `kdive.services`
and reads no DB / object-store credential — scoped to the entire CLI package so the
boundary cannot erode through the transport or login modules.

### Packaging

A `kdivectl` `console_scripts` entry point in the existing `kdive` wheel/image — no
second distributable. The CLI already has the config registry and auth code it needs
in-package.

### Out of scope (per the band design)

- **`doctor` / observability** — M2.3. `kdivectl` is the vehicle the `doctor` verb
  will hang off, so the core is designed to host later verbs, but no `doctor` ships
  here.
- **Image / rootfs lifecycle verbs** — M2.4 (co-designed with that subsystem).
- **Real-IdP interactive login** — later milestone.

## Command surface (M2.2)

```
kdivectl login --platform-role <role>      # mock OIDC flow + token cache; prod uses KDIVE_TOKEN
kdivectl tool call <mcp-tool> --json '…'   # passthrough — read-only tools only (fail-closed)

# read-only (backed by existing tools)
kdivectl resources list|describe
kdivectl allocations list|get
kdivectl systems list|show
kdivectl runs list|show
kdivectl jobs list|get
kdivectl ledger show                       # accounting usage/reports
kdivectl inventory show                    # object-store wiring

# read-only (NET-NEW MCP tools)
kdivectl secrets list                      # secret *presence*, never values — PLATFORM-ROLE-GATED, operator-only
kdivectl fixtures list                     # rootfs/fixture catalog view — project-scoped (agents too)

# mutating / destructive (break-glass path, audited)
kdivectl teardown --project … --force      # cross-project teardown
kdivectl allocations force-release <id>
kdivectl resources cordon|drain <host>
```

Most read verbs map to tools that already exist (`resources.list/describe`,
`allocations.get/list`, `jobs.list/get`, `inventory.list`, accounting
usage/reports). The genuine net-new MCP tools are **`secrets.list`** (a
`secret_registry` exists but exposes no presence tool) and **`fixtures.list`** (the
rootfs/fixture catalog has no read tool today). Their authz scopes differ
deliberately (see ADR-0089 decision 6): **`secrets.list` is platform-role-gated and
off the general agent surface** — secret *presence* (which refs exist, across
projects and the platform) is a reconnaissance primitive, so exposing it to every
tenant agent is the risk, not the goal; it returns presence/metadata only, never
values. **`fixtures.list` is project-scoped** like the other catalog reads (an agent
sees its projects' fixtures, an operator the platform view); the fixture catalog is
not sensitive the way secret presence is.

**Coverage of "every domain object" (the exit criterion).** The curated verbs above
cover the high-traffic operator objects. Domain objects without a curated verb
(projects, hosts, shapes, investigations, artifacts, vmcore targets) are inspected via
`kdivectl tool call <their read tool>` — which, because the passthrough is read-only
fail-closed, requires each such read tool to carry the `mutating: false` annotation.
Issue 3 owns annotating those existing read tools so the passthrough reaches them; that
is what makes "lists/inspects every domain object" falsifiable — a checklist of every
domain read tool either has a curated verb or is reachable (annotated) via `tool call`,
and the exit test enumerates it.

## Components & isolation

| Unit | Purpose | Depends on |
|------|---------|-----------|
| `kdive.cli.transport` | MCP-client session: URL resolution, token attach, call, error→exit-code | config registry, MCP client SDK |
| `kdive.cli.login` | mock-OIDC auth-code flow, `--platform-role` principal, `0600` token cache | `transport`; flow logic promoted from `tests/integration/live_stack/harness.py` |
| `kdive.cli.render` | table / `--json` output | — |
| `kdive.cli.commands.*` | curated verbs (read-only, then mutating) | `transport`, `render` |
| `mcp/tools/ops/secrets.py` | net-new `secrets.list` presence tool | `secret_registry`, redaction |
| `mcp/tools/catalog/fixtures.py` | net-new `fixtures.list` catalog tool | fixture catalog |

Each unit is reachable without reading the others' internals; the whole `kdive.cli.*`
package (not just `commands.*`) never imports `kdive.services` and reads no DB /
object-store credential — enforced on the import graph (see Testing), so the boundary
cannot erode through the transport or login modules where a shortcut would be tempting.

## Decomposition (epic + 7 sub-issues)

Read-only lands first; ordering shows hard dependencies, the rest parallelize.

0. **Server-side audit attribution** — the milestone's only non-`kdive.cli` change: the
   audit middleware records the caller's `client_id`/`azp` and resolves `actor` from the
   closed map (`kdivectl`-client → `operator-cli`; recognized agent client + `agent_session`
   → `agent`; `actor=unknown` otherwise, never defaulting to `agent`). Extends the OIDC test
   harness (`mint_token` in `tests/integration/live_stack/harness.py`) to mint under a
   distinct `kdivectl` `client_id` so the map is exercisable. *(foundation; no `kdive.cli`
   dependency — can land in parallel with 1)*
1. **Generic MCP-client core** — `kdive.cli` skeleton, `kdivectl` entry point,
   config-registry server URL, token attach (`KDIVE_TOKEN` / cred file), read-only
   fail-closed `tool call` passthrough (allow only `mutating: false`-annotated tools;
   annotates one read tool so its own round-trip test passes without waiting on issue 3),
   `--json`, error→exit-code mapping. *(foundation)*
2. **`kdivectl login`** — mock-OIDC auth-code flow, `--platform-role <role>`, token
   cache written `0600` (parent dir `0700`; token never logged, redacted from error
   output); BYO-token path. *(depends on 1)*
3. **Read-only verbs (existing-tool-backed)** — resources/allocations/systems/runs/
   jobs/ledger/inventory + table renderers. Also annotates every domain *read* tool
   `mutating: false` so the passthrough reaches the objects without a curated verb
   (the "every domain object" coverage above). *(depends on 1; parallel with 2, 4)*
4. **Net-new read tools** — `secrets.list` (presence, **platform-role-gated /
   operator-only**) + `fixtures.list` (project-scoped) MCP tools, then their `kdivectl`
   verbs; redaction-aware. *(depends on 1)*
5. **Mutating/destructive verbs** — teardown / force-release / cordon / drain via the
   break-glass path. Each does a pre-flight token-`exp` check and **fails closed** rather
   than risk a mid-operation 401, and is specified **single-call and re-runnable**
   (idempotent against already-released/already-torn-down state, which the reconciler's
   drift-repair already tolerates). *(depends on 1, after 3)*
6. **Boundary test + operator runbook** — the denied-and-audited exit-criterion
   proof; help/output polish; runbook entry. *(depends on 0, 2, 3, 5)*

## Testing & exit criteria

- **Boundary proof (load-bearing).** Drive one read verb and one mutating verb twice:
  once with `platform_admin` (succeeds; audit row carrying the `kdivectl` client id →
  `actor=operator-cli`), once with **no platform role** (**denied** *and* an audit row).
  This proves the *platform* boundary every mutating verb gates on — not a project-role
  boundary that could pass for the wrong reason — and is issue 6's core.
- **No-bypass guard.** A structural test asserting the whole `kdive.cli.*` package
  (transport and login included, not just `commands.*`) imports no `kdive.services.*`
  module and reads no DB / object-store settings — asserted on the actual import graph,
  **not** a string grep (a grep guard goes vacuous after a rename).
- **Output contract.** Every curated verb has a `--json` golden test (stable for
  scripting) and a table smoke test.
- **Net-new tools.** `secrets.list` returns presence only — a redaction test proving
  no secret value ever crosses the wire (reuses the existing redaction guards) — and a
  gating test proving a non-platform principal is denied.
- **Generic passthrough.** `tool call` against a known read tool round-trips; an
  unknown tool yields a clean error and nonzero exit; and a **mutating tool is refused**
  client-side (the read-only fail-closed guard), proving the passthrough is not an
  escape hatch around break-glass.
- **Token at rest.** The `login` cache file is created `0600`; a redaction test proves
  the token is absent from logs and error output.

### Milestone exit criterion (from the band design)

> `kdivectl` lists/inspects every domain object and reports secret *presence* under an
> authenticated principal; an unauthenticated or under-privileged invocation is
> **denied and audited**.

## Consequences

- No new transport or front door: the operator surface is the same FastMCP server,
  same auth, same audit. RBAC is only as strong as the IdP's role mapping — stated
  here because dev (mock issues any claims) and prod (real IdP role mapping) diverge
  entirely at the IdP, not in kdive.
- `kdivectl` becomes the delivery vehicle for the M2.3 `doctor` verb and the M2.4
  image-lifecycle verbs; its core is designed to host them without rework.
- The MCP tool surface gains two read-only tools. `fixtures.list` is project-scoped
  and agents gain it too (consistent with the agent-native principle). `secrets.list`
  is the deliberate exception: platform-role-gated and operator-only, because secret
  *presence* is an information-disclosure surface that should not reach tenant agents.
