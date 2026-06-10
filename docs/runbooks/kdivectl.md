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
kdivectl allocations list [--project <project>]
kdivectl allocations get <allocation_id>
kdivectl systems list [--state <state>]
kdivectl systems show <system_id>
kdivectl runs show <run_id>
kdivectl jobs list
kdivectl jobs get <job_id>
kdivectl ledger show [--project <project>]
kdivectl inventory show [--project <project>]
```

`--json` may be given before or after the verb (`kdivectl --json resources list` or
`kdivectl resources list --json`) for a stable, scriptable contract.

### Secret-presence and fixture reads

Two reads surface catalog presence without exposing values. `secrets list` is
platform-role gated; `fixtures list` is a plain authenticated read:

```bash
kdivectl secrets list                       # secret *presence* (refs only), platform-gated
kdivectl fixtures list [--project <project>] # available fixtures, project-scoped
```

`secrets list` reports presence/refs only — it never returns secret values.

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
