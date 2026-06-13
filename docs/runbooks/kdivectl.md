# Runbook: `kdivectl` operator CLI

Operator guide for `kdivectl`, the kdive admin CLI. `kdivectl` is a FastMCP **client**: it
attaches an OIDC bearer token and calls the same MCP tools an agent does — there is no new
server transport and no direct database or object-store access from the operator host (the
host holds only the bearer token). Curated verbs render read tools as tables/JSON; a
fail-closed read-only `tool call` passthrough reaches the rest of the surface; the mutating
verbs route through the M1.3 break-glass tools. See
[ADR-0089](../adr/0089-operator-cli-mcp-client.md) and the
[M2.2 plan](../superpowers/plans/2026-06-10-m22-admin-cli.md).

Every call `kdivectl` makes is attributed: the server records the OIDC `client_id` and
resolves an `actor`. When you authenticate under the dedicated `kdivectl` OIDC client, your
actions are audited as **`operator-cli`** — distinct from an agent's `agent` actor. Reading
the audit trail by `actor` is how you separate operator break-glass from routine agent work
(see [Reading the audit trail](#reading-the-audit-trail-by-actor)).

## Prerequisites

1. **A dedicated `kdivectl` OIDC client.** Register a client in your IdP whose id is
   recorded as the CLI's `azp`/`client_id`. The server maps this client id to
   `actor=operator-cli`. The default is `kdivectl`; override with `KDIVE_CLI_CLIENT_ID` if
   you registered a different id.
2. **Environment.** `kdivectl` reads its configuration from `KDIVE_*` environment variables
   (the single config source of truth, ADR-0087):

   | variable | purpose | default |
   |----------|---------|---------|
   | `KDIVE_SERVER_URL` | the server's streamable-HTTP MCP endpoint | `http://127.0.0.1:8080/mcp` |
   | `KDIVE_TOKEN` | bearer token (prod path; overrides the login cache) | unset |
   | `KDIVE_CLI_CLIENT_ID` | OIDC `client_id` the CLI authenticates under | `kdivectl` |
   | `KDIVE_OIDC_ISSUER` | mock-OIDC issuer base URL (dev `login` path) | unset |
   | `KDIVE_OIDC_AUDIENCE` | token audience (dev `login` path) | the server default |

   Point `KDIVE_SERVER_URL` at the running stack's MCP endpoint (for a local stack that is
   typically `http://127.0.0.1:8000/mcp` — keep it in sync with the bind address; see
   [live-stack.md](live-stack.md)).
3. **Install.** `kdivectl` is the `kdivectl` console script from this package
   (`pip install kdive` / `uv pip install kdive`), or run it in-tree as
   `python -m kdive.cli`.

## Authenticating

There are two token paths; `KDIVE_TOKEN` always wins over the login cache.

### Production: supply `KDIVE_TOKEN`

Have your IdP mint an access token for an operator principal under the `kdivectl` client
and export it:

```bash
export KDIVE_TOKEN="$(your-idp-mint-operator-token)"
export KDIVE_SERVER_URL="https://kdive.example.com/mcp"
kdivectl resources list
```

`kdivectl` never prints or logs the token.

### Development: `kdivectl login` against the mock-OIDC issuer

With `KDIVE_OIDC_ISSUER` set, `kdivectl login` drives the mock-OIDC authorization-code flow,
mints a token under `KDIVE_CLI_CLIENT_ID`, and caches it `0600` (under a `0700` parent) at
`$XDG_STATE_HOME/kdive/token` (default `~/.local/state/kdive/token`). The cached token is
read automatically when `KDIVE_TOKEN` is unset.

```bash
export KDIVE_OIDC_ISSUER="http://127.0.0.1:8081/default"
export KDIVE_SERVER_URL="http://127.0.0.1:8000/mcp"

kdivectl login                              # no platform role
kdivectl login --platform-role platform_operator
kdivectl login --platform-role platform_admin
```

The `--platform-role` axis encodes the platform role into the minted token. Break-glass
mutating verbs need the platform role the underlying tool gates on — see
[Break-glass mutating verbs](#break-glass-mutating-verbs).

## Read verbs

Curated read verbs call one read-only MCP tool and render a table (or JSON with `--json`):

```bash
kdivectl resources list [--kind <kind>]
kdivectl resources describe <resource_id>
kdivectl allocations list --project <project>
kdivectl allocations get <allocation_id>
kdivectl systems list [--state <state>]
kdivectl systems show <system_id>
kdivectl runs show <run_id>
kdivectl jobs list
kdivectl jobs get <job_id>
kdivectl ledger show --project <project>
kdivectl inventory show [--project <project>]
```

`--json` may be given before or after the verb (`kdivectl --json resources list` or
`kdivectl resources list --json`) for a stable, scriptable contract.

`--project` is **required** for `allocations list` and `ledger show` (no square brackets):
each underlying tool (`allocations.list`, `accounting.usage_project`) reads exactly one
project, so the CLI enforces the flag up front — omitting it is a usage error (exit `2`),
not a cross-project listing. `inventory show` is the exception: its `--project` is an
**optional** narrowing filter on a cross-project auditor read (`inventory.list`, see
[the matrix below](#read-authorization-platform-axis-vs-project-axis)), omitted for the
all-projects view. There is no "list across all my projects" verb today; query each project
in turn.

### Read authorization: platform axis vs. project axis

Read verbs split across the two independent authorization axes (ADR-0043 §7), and the split
is **load-bearing**: a platform role does **not** grant project-scoped reads, and project
membership does **not** grant cross-project reads. A `kdivectl login --platform-role …` token
with no project grant sees no project tenant data — there is no implicit "admin sees
everything." To read a specific project's data, be granted on that project; for the
cross-project oversight view, use a `platform_auditor` token.

| read | authorized by | denied to |
|------|---------------|-----------|
| `allocations list/get`, `systems list/show`, `runs show`, `jobs list/get`, `ledger show` (`accounting.usage_project`) | per-project `viewer` on the **target project** (`require_role`) | a platform-only token with no membership on that project sees no project tenant data. A by-id `get`/`show` returns a **not-found-shaped** result (exit `4`; tenant existence is not revealed, and **no** distinct authorization-denied code is emitted). A read that **names a project** the caller is not a member of (`allocations list --project …`, `ledger show` / `accounting.usage_project`, `accounting.estimate`) is denied `authorization_denied` (**exit `3`**, ADR-0098) — the named project carries no existence to leak, so the denial surfaces distinctly (ADR-0043 §4a) |
| cross-project `inventory show` (`inventory.list`), `accounting.report` (all-projects), `audit.query` (cross-project) | `platform_auditor` (satisfied by `platform_admin`) | a project-member token holding no platform role |
| `secrets list`, `doctor` | `platform_operator` | any token lacking `platform_operator` |
| `resources list/describe`, `fixtures list` | plain authenticated read (no project scope, no role floor) | unauthenticated callers only |

Note `inventory show` is the **cross-project auditor** read (it maps to the `inventory.list`
tool, gated `platform_auditor`), not a per-project read — it is the one read verb where a
platform-axis token is *granted* and a bare project member is *denied*. Every other project-data
read is the inverse.

Three project-axis outcomes are distinct and should not be conflated. (1) A **non-member**
(including a platform-only token) reaching a **by-id** `get`/`show` gets the
**not-found-shaped** result above (exit `4`) — the tool resolves the object's project, finds the
caller is not a member, and returns not-found *before* the role check, so a non-grant never
surfaces a distinct authorization-denied code (and is **not** audited; only platform-role
*overreach* within the platform tier leaves a denial row — ADR-0043 §4, see
[Reading the audit trail](#reading-the-audit-trail-by-actor)). The distinction is deliberate: a
by-id lookup must not become a cross-tenant existence oracle, so "ungranted, exists" is
indistinguishable from "absent". (2) A **non-member naming a project** in a named-scope read/op
(`allocations list --project`, `accounting.usage_project`, `accounting.estimate`) is denied
`authorization_denied` (**exit `3`**, ADR-0098) — the caller already supplied the project name, so
there is no existence to hide, and the denial surfaces distinctly rather than collapsing to a
generic error; like the by-id non-grant it is **not** audited (the non-member case is
non-amplifying). (3) A **member** whose role ranks below the required floor reaches `require_role`,
which surfaces `authorization_denied` (**exit `3`**) **and** is audited as a member-over-reach
denial.

### Secret-presence and fixture reads

Two reads surface catalog presence without exposing values. `secrets list` is
platform-role gated; `fixtures list` is a plain authenticated read:

```bash
kdivectl secrets list                       # secret *presence* (refs only), platform-gated
kdivectl fixtures list                       # available fixtures, plain authenticated read
```

`secrets list` reports presence/refs only — it never returns secret values.

## Diagnostics (`doctor`)

`doctor` runs the read-only deployment diagnostics through the operator-gated
`ops.diagnostics` tool and renders one verdict row per check (`check`, `status`, `detail`,
`fix`, `provider`). It is usable as a deployment/CI gate as well as interactively:

```bash
kdivectl doctor                              # the three cheap read checks (default)
kdivectl doctor --provider remote-libvirt    # diagnose one named registered provider
kdivectl doctor --with-egress                # also run the heavyweight egress probe
kdivectl doctor --json                       # the verdict rows as stable JSON
```

The exit code is **gate-safe** (ADR-0091 §5): all-`pass` exits `0`; any `fail` exits `1`
(a contract is violated, and `fix` names the remediation); a check that could not run to a
verdict (a down dependency) is reported as `error` and exits `6` — a *distinct* nonzero code,
so a gate never goes green on a check that did not actually pass. A `fail` and an `error`
together exit `1` (a real contract violation is never masked by an unrelated down
dependency). `doctor` is operator-gated, so it exits `3` if your token lacks
`platform_operator`. The default run is the three read checks; `--with-egress` is opt-in
because the egress probe provisions a probe guest.

## Read-only passthrough (`tool call`)

To reach any read-only MCP tool not covered by a curated verb, use the passthrough. It is
**fail-closed**: it lists the server's tools and refuses any tool not annotated
`readOnlyHint`, so it can never reach a mutating tool — the curated break-glass verb is the
only path to those.

```bash
kdivectl tool call accounting.usage_project --json '{"project": "my-proj"}'
```

A non-read-only target exits `3` without calling the tool.

## Break-glass mutating verbs

These verbs route through the M1.3 break-glass tools. Each is single-call and re-runnable
(the server tools are idempotent against already-torn-down/already-released state). Before
its one call, each verb runs a fail-closed token-`exp` preflight: a near-expired token is
refused up front (re-run `kdivectl login` and retry) rather than risking a mid-operation
401. These tools are `destructive()`-annotated server-side, so the read-only passthrough
cannot reach them.

```bash
kdivectl teardown system <system_id> --reason <R> --force   # ops.force_teardown (needs --force)
kdivectl allocations force-release <allocation_id> --reason <R>  # ops.force_release
kdivectl resources cordon <resource_id>                     # resources.cordon
kdivectl resources drain <resource_id> [--mode passive|force_release] [--reason <R>]  # resources.drain
```

**Platform roles required (these are not implied by one another):**

| verb | gated on |
|------|----------|
| `teardown system`, `allocations force-release` | `platform_admin` |
| `resources cordon` | `platform_operator` |
| `resources drain --mode passive` (default) | `platform_operator` |
| `resources drain --mode force_release` | `platform_admin` (it empties tenant allocations) |

`platform_admin` does **not** imply `platform_operator`, and vice versa — authenticate with
the platform role the specific verb gates on. A verb invoked without the required platform
role exits `3` (`authorization_denied`), and — when your token holds *some* platform role —
the denial is itself audited under `actor=operator-cli` (separation-of-duties accountability).

`teardown system` additionally requires `--force` as an explicit break-glass acknowledgement.

### Exit codes

| code | meaning |
|------|---------|
| `0` | success |
| `1` | generic failure |
| `2` | configuration error |
| `3` | authorization denied (or a non-read-only passthrough target) |
| `4` | not found |
| `5` | conflict |
| `6` | `doctor` only: a check could not run to a verdict (`error`); not a passed contract |

## Reading the audit trail by `actor`

Every break-glass call writes a `platform_audit_log` row carrying the `tool`, a `scope`
(the target project and object id), a one-way digest of the arguments (the `reason` is
digested, never stored in plaintext), the held `platform_role`, and the resolved `actor`.
When you authenticate under the `kdivectl` client, that `actor` is `operator-cli`.

To review operator break-glass activity, filter by `actor` against the stack's Postgres:

```sql
SELECT ts, principal, tool, scope, platform_role, actor
FROM platform_audit_log
WHERE actor = 'operator-cli'
ORDER BY ts DESC;
```

Both successful break-glass calls and audited denials appear here, so the trail is a
complete operator accountability record. (A denial from a token holding *no* platform role
is the routine non-grant case and is deliberately not recorded; only platform-role overreach
leaves a denial row.)

## Exit-criterion boundary test

`tests/integration/test_kdivectl_boundary.py` is the load-bearing proof of the above: it
drives `kdivectl allocations force-release` through the real entry point twice — once with a
`platform_admin` token (succeeds; `operator-cli` audit row) and once with an under-privileged
`platform_operator` token (exit `3` + a `operator-cli` denial audit row). It is gated
`live_stack`, so it runs only against a running stack (`just stack-up` + the app tier, then
`just test-live-stack`) and skips cleanly in normal CI. Running it is part of this runbook,
not the CI gate.
