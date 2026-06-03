# ADR 0010 — FastMCP server framework + streamable-HTTP auth integration

- **Status:** Proposed
- **Date:** 2026-06-03
- **Refines:** [0002](0002-multi-user-mcp-http.md) (MCP-HTTP transport + bearer
  model) and the spec's open follow-up "MCP Python server framework and
  streamable-HTTP auth integration specifics".

## Context

[0002](0002-multi-user-mcp-http.md) decides *what* the transport and auth model
are — MCP over streamable HTTP with OIDC bearer tokens — but leaves the *framework*
that realizes the tool surface, session handling, and bearer validation open. The
MCP tool surface is large (discovery, allocation, provisioning, build/install,
debug, control/retrieve, jobs) and every tool must return structured JSON with
object id, status, `suggested_next_actions`, and artifact references — never log
dumps. See the spec's "MCP tool surface".

## Decision

We will build the MCP tool surface with **FastMCP 3.x (`prefecthq/fastmcp`)**
served over its **streamable-HTTP transport** (`transport="http"`). Bearer tokens
are validated by FastMCP's **`JWTVerifier`**, configured against the OIDC
provider's JWKS URI and expected audience; the verified token's claims yield the
`(principal, agent_session)` request context that tools read for authz, audit, and
accounting. The verifier **must enforce issuer (`iss`) and audience (`aud`)** per
[0002](0002-multi-user-mcp-http.md); if a `JWTVerifier` build does not check `iss`
natively, an auth middleware adds the check rather than relaxing the invariant.
The **same token validation backs the human REST/gRPC surface**
([0002](0002-multi-user-mcp-http.md)) through shared middleware, outside FastMCP.
Tools are decorator-defined and return structured JSON (object id, status,
`suggested_next_actions`, artifact references).

## Consequences

- FastMCP provides streamable HTTP, decorator tools, automatic schema generation,
  and built-in JWT bearer verification, so the server skeleton stays small.
- Adds a FastMCP dependency (and the official MCP SDK it builds on); the exact
  version is pinned in `pyproject.toml` and upstream auth changes are tracked.
- `JWTVerifier` covers resource-server token validation; token *issuance* stays
  with the external IdP, matching [0002](0002-multi-user-mcp-http.md) and
  [0006](0006-oidc-rbac-attribution.md).
- The `(principal, agent_session)` plumbing is centralized in one auth layer; tool
  bodies stay free of auth boilerplate.

## Alternatives considered

- **The official MCP Python SDK directly.** Rejected: more boilerplate for tool
  registration, session, and schema; FastMCP is a thin, maintained layer over it
  and preserves the protocol semantics.
- **A custom ASGI app over the raw MCP protocol.** Rejected: reimplements transport
  and schema handling for no gain.
- **FastMCP 1.x (the version merged into the official SDK).** Rejected: the
  standalone 2.x→3.x `prefecthq/fastmcp` line is the actively developed one with
  first-class streamable-HTTP and auth; pinning the lineage avoids ambiguity.
