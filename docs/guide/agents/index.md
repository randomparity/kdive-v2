# Agent onboarding

This guide connects an MCP client (Claude Code or Claude Desktop) to a running
KDIVE server and walks through a first set of tool calls.

Two example configs ship alongside this page:

- [`mcp.json`](mcp.json) — a Claude Code project `.mcp.json`.
- [`claude_desktop_config.json`](claude_desktop_config.json) — a Claude Desktop
  config file.

Both point at the placeholder endpoint `https://kdive.internal/mcp`. Replace it
with your deployment's URL and supply a token (see
[Authentication](#authentication)).

## Where each config file goes

### Claude Code

Copy [`mcp.json`](mcp.json) to a file named `.mcp.json` in your project root.
Claude Code reads it at startup and the entry is shared with everyone who checks
out the repository.

Claude Code connects to remote MCP servers over HTTP natively, so the entry uses
the streamable-HTTP transport directly:

```json
{
  "mcpServers": {
    "kdive": {
      "type": "http",
      "url": "https://kdive.internal/mcp",
      "headers": { "Authorization": "Bearer ${KDIVE_TOKEN}" }
    }
  }
}
```

`${KDIVE_TOKEN}` is expanded from the environment when Claude Code loads the
file, so the token itself is never written into the config.

### Claude Desktop

Merge [`claude_desktop_config.json`](claude_desktop_config.json) into the Claude
Desktop config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Restart Claude Desktop after editing the file.

Claude Desktop launches MCP servers as `stdio` child processes and has no
built-in HTTP client, so it cannot take a `url` field the way Claude Code does.
The example bridges to the remote endpoint with `mcp-remote`, which wraps the
HTTP transport in a stdio session and injects the bearer token as a header:

```json
{
  "mcpServers": {
    "kdive": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://kdive.internal/mcp",
        "--header",
        "Authorization:${KDIVE_AUTH_HEADER}"
      ],
      "env": {
        "KDIVE_AUTH_HEADER": "Bearer ${KDIVE_TOKEN}"
      }
    }
  }
}
```

The `Bearer ` prefix lives in the `KDIVE_AUTH_HEADER` environment variable rather
than the `args` array, and the `--header` value has no space around the colon.
This avoids a Claude Desktop bug on Windows that mangles spaces inside `args`
when it invokes `npx`. `npx` fetches `mcp-remote` on first use, so the host
needs Node.js installed and network access.

## Authentication

KDIVE validates an OIDC-issued bearer token on every call; there is no anonymous
access. The server checks the token's issuer, audience, and signature against the
`KDIVE_OIDC_ISSUER`, `KDIVE_OIDC_AUDIENCE`, and `KDIVE_OIDC_JWKS_URI` it was
configured with (see the
[configuration reference](../reference/config.md#oidc)).

Obtain a token from your identity provider for the audience the server expects,
then expose it to the client as `KDIVE_TOKEN`:

```bash
export KDIVE_TOKEN="<oidc-access-token>"
```

The token is short-lived; refresh `KDIVE_TOKEN` and reconnect when it expires. A
`401` from the server means the token is missing, expired, or issued for the
wrong audience.

## First-call smoke sequence

Once the server is connected, confirm the tools are reachable with a minimal
flow. Tool names below are the namespaced identifiers from the
[tool reference](../reference/index.md).

1. `investigations.open` — open an Investigation under your project. Keep the
   returned investigation id; later calls scope to it.
2. `allocations.request` — request an allocation (size, lease window, resource
   selector). This returns a job; allocation is asynchronous.
3. `jobs.wait` — wait on the job id from the previous step until it reaches a
   terminal state.
4. `allocations.get` — read back the granted allocation once the job succeeds.

From there, drive a run with the `runs.*` tools and read results with the
`vmcore.*` and `debug.*` tools. The full surface is listed in the
[tool reference](../reference/index.md).
