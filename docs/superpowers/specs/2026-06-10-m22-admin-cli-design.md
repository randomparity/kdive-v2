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
`JWTVerifier`, the RBAC gate, and `DenialAuditMiddleware` (audit) unchanged. kdive
stays a pure verifier; `kdivectl` holds only the bearer token the IdP minted for the
human running it.

### Three layers

1. **Generic client core** (`kdive.cli`). Resolves the server URL from the config
   registry, attaches the bearer token (from `kdivectl login`'s cache, or
   `KDIVE_TOKEN` / a credentials file), maps MCP tool-errors to CLI exit codes, and
   exposes a raw `kdivectl tool call <name> --json '{…}'` passthrough so the **entire
   MCP surface is reachable by humans** — the broad "features humans currently reach
   through GUIs" win, at near-zero cost once the substrate exists.

2. **Curated operator verbs**, layered on the core, operator-shaped (human table by
   default, `--json` on every command). Read-only verbs land first (low risk).
   Mutating verbs route through the **M1.3 break-glass path**
   (`mcp/tools/ops/breakglass.py`, `services/allocation/release.py`) — **not** the
   per-allocation iteration gate (`security/authz/gate.py`), which is
   allocation/profile-scoped for an agent iterating, not an operator administering.

3. **`kdivectl login`.** Drives the mock-OIDC authorization-code flow with a
   parameterized principal (`--as admin|operator|viewer`) for dev/CI and the boundary
   test, then caches the token. Prod brings its own token via `KDIVE_TOKEN` or a
   credentials file. A full real-IdP interactive flow (PKCE / device-code) is
   **deferred** — the RBAC-boundary exit test needs only the mock's
   parameterized-principal capability.

### Boundary invariant (the milestone's spine)

Every `kdivectl` action is an authenticated MCP call, attributed in the audit log
under `(principal, operator-cli)` exactly as MCP tools are. An under-privileged
principal is **denied and audited** — proven by test, not assumed. There is no
`kdive.services` import that bypasses the token and no DB / object-store credential
on the operator host.

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
kdivectl login --as <principal>            # mock OIDC flow + token cache; prod uses KDIVE_TOKEN
kdivectl tool call <mcp-tool> --json '…'   # generic passthrough — whole MCP surface

# read-only (backed by existing tools)
kdivectl resources list|describe
kdivectl allocations list|get
kdivectl systems list|show
kdivectl runs list|show
kdivectl jobs list|get
kdivectl ledger show                       # accounting usage/reports
kdivectl inventory show                    # object-store wiring

# read-only (NET-NEW MCP tools — agents benefit too)
kdivectl secrets list                      # secret *presence*, never values
kdivectl fixtures list                     # rootfs/fixture catalog view

# mutating / destructive (break-glass path, audited)
kdivectl teardown --project … --force      # cross-project teardown
kdivectl allocations force-release <id>
kdivectl resources cordon|drain <host>
```

Most read verbs map to tools that already exist (`resources.list/describe`,
`allocations.get/list`, `jobs.list/get`, `inventory.list`, accounting
usage/reports). The genuine net-new MCP tools are **`secrets.list`** (a
`secret_registry` exists but exposes no presence tool) and **`fixtures.list`** (the
rootfs/fixture catalog has no read tool today). Both are read-only and benefit
agents as much as operators.

## Components & isolation

| Unit | Purpose | Depends on |
|------|---------|-----------|
| `kdive.cli.transport` | MCP-client session: URL resolution, token attach, call, error→exit-code | config registry, MCP client SDK |
| `kdive.cli.login` | mock-OIDC auth-code flow, `--as` principal, token cache | `transport`; flow logic promoted from `tests/integration/live_stack/harness.py` |
| `kdive.cli.render` | table / `--json` output | — |
| `kdive.cli.commands.*` | curated verbs (read-only, then mutating) | `transport`, `render` |
| `mcp/tools/ops/secrets.py` | net-new `secrets.list` presence tool | `secret_registry`, redaction |
| `mcp/tools/catalog/fixtures.py` | net-new `fixtures.list` catalog tool | fixture catalog |

Each unit is reachable without reading the others' internals; `commands.*` never
imports `kdive.services` (enforced — see Testing).

## Decomposition (epic + 6 sub-issues)

Read-only lands first; ordering shows hard dependencies, the rest parallelize.

1. **Generic MCP-client core** — `kdive.cli` skeleton, `kdivectl` entry point,
   config-registry server URL, token attach (`KDIVE_TOKEN` / cred file), `tool call`
   passthrough, `--json`, error→exit-code mapping. *(foundation)*
2. **`kdivectl login`** — mock-OIDC auth-code flow, `--as <principal>`, token cache;
   BYO-token path. *(depends on 1)*
3. **Read-only verbs (existing-tool-backed)** — resources/allocations/systems/runs/
   jobs/ledger/inventory + table renderers. *(depends on 1; parallel with 2, 4)*
4. **Net-new read tools** — `secrets.list` (presence) + `fixtures.list` MCP tools,
   then their `kdivectl` verbs; redaction-aware. *(depends on 1)*
5. **Mutating/destructive verbs** — teardown / force-release / cordon / drain via the
   break-glass path. *(depends on 1, after 3)*
6. **Boundary test + operator runbook** — the denied-and-audited exit-criterion
   proof; help/output polish; runbook entry. *(depends on 2, 3, 5)*

## Testing & exit criteria

- **Boundary proof (load-bearing).** Drive one read verb and one mutating verb twice:
  once `login --as admin` (succeeds; audit row under `(principal, operator-cli)`),
  once `--as viewer` (**denied** *and* an audit row). This is the M2.2 exit criterion
  verbatim and issue 6's core.
- **No-bypass guard.** A structural test asserting `kdive.cli.commands.*` imports no
  `kdive.services.*` module and reads no DB / object-store settings — asserted on the
  actual import graph, **not** a string grep (a grep guard goes vacuous after a
  rename).
- **Output contract.** Every curated verb has a `--json` golden test (stable for
  scripting) and a table smoke test.
- **Net-new tools.** `secrets.list` returns presence only — a redaction test proving
  no secret value ever crosses the wire (reuses the existing redaction guards).
- **Generic passthrough.** `tool call` against a known read tool round-trips; an
  unknown tool yields a clean error and nonzero exit.

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
- The MCP tool surface gains two read-only tools (`secrets.list`, `fixtures.list`)
  that agents gain too — consistent with the agent-native principle (operators get no
  capability agents lack).
