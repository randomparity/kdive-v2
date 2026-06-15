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

> **Claude Code reads `${KDIVE_TOKEN}` once, at startup.** It is expanded when the
> MCP server is loaded, not per call — so export the token *before* launching
> Claude Code (or before the `kdive` server connects), and when it expires,
> re-export and reconnect the server. A token exported in a shell *after* Claude
> Code is already running does not reach an already-loaded MCP entry.

## Connecting to a bundled-backends demo (Kubernetes)

The `values-demo.yaml` deployment (in-chart Postgres/MinIO/mock-OIDC) does **not**
give you the stable `url` and external IdP this page otherwise assumes. Two demo
specifics replace them:

1. **There is no public URL — port-forward to localhost.** The demo Service is
   `ClusterIP` (the bundled issuer mints valid tokens for any caller, so it must
   never be exposed). Run a port-forward and keep it running for the whole session:

   ```bash
   kubectl -n <namespace> port-forward svc/<release>-server 8000:8000
   ```

   Then set the `.mcp.json` `url` to `http://127.0.0.1:8000/mcp` (not a remote
   host). If the forward stops, every call fails until you restart it.

2. **Mint the token from inside the cluster.** The mock issuer is in-cluster only,
   and the token's `iss` must be the in-cluster issuer URL the server validates
   against — so mint it via `kubectl exec` into the server pod, not from your
   workstation (a port-forwarded mint stamps the wrong `iss` and 401s). The
   bundled issuer also mints `aud=kdive` tokens for any caller, so no real IdP is
   involved. From a source checkout a helper wraps the mint:

   ```bash
   export KDIVE_TOKEN=$(scripts/demo-token.sh)   # KDIVE_DEMO_{NAMESPACE,FULLNAME,CONTEXT} override
   ```

   (the raw `kubectl exec` form is also printed in the release notes, `helm get notes
   <release>`, and the [Kubernetes deploy runbook](../../operating/runbooks/kubernetes-deploy.md)).
   Export it **before** launching Claude Code — it reads `${KDIVE_TOKEN}` once at startup.
   Demo tokens expire after ~1 hour: re-run the helper, re-export, and reconnect the server.

Driving a **remote-libvirt** host (e.g. a `qemu+tls://` target) from this demo is
the same tool flow below — the host just has to be registered in the deployment's
`systems.toml` inventory and reachable from the worker; see the
[remote-libvirt host-setup runbook](../../operating/runbooks/remote-libvirt-host-setup.md).
The worker pod is a lightweight app process, so a from-source `runs.build` needs a
registered build host (runbook §"Offloading the from-source build to an ephemeral
build VM"); `host_dump` capture and provisioning do not.

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
