"""``KDIVE_*`` settings the operator CLI (`kdivectl`) reads (ADR-0089).

The operator host holds only the bearer token and the server URL; it never reads
database or object-store credentials (ADR-0089 decision 5). These settings are
declared here and aggregated through the config manifest like every other group, so
the generated reference stays complete.
"""

from __future__ import annotations

from kdive.config.registry import Setting


def _str(raw: str) -> str:
    return raw


SERVER_URL = Setting(
    name="KDIVE_SERVER_URL",
    parse=_str,
    default="http://127.0.0.1:8080/mcp",
    group="cli",
    help="MCP server URL kdivectl connects to.",
    suggest="the server's streamable-HTTP MCP endpoint, e.g. http://host:8080/mcp",
)
TOKEN = Setting(
    name="KDIVE_TOKEN",  # pragma: allowlist secret - env var name, not a value
    parse=_str,
    default=None,
    secret=True,
    group="cli",
    help="Bearer token kdivectl presents (prod path; overrides the login cache).",
    suggest="a bearer token your IdP minted for an operator principal",
)
CLI_CLIENT_ID = Setting(
    name="KDIVE_CLI_CLIENT_ID",
    parse=_str,
    default="kdivectl",
    group="cli",
    help="OIDC client_id kdivectl authenticates under (recorded as actor=operator-cli).",
    suggest="the dedicated kdivectl OIDC client id registered in your IdP",
)

SETTINGS = [SERVER_URL, TOKEN, CLI_CLIENT_ID]
