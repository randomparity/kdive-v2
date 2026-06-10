"""kdivectl's MCP-client session: resolve URL, attach bearer token, call a tool (ADR-0089).

Imports NO ``kdive.services`` and reads NO DB/object-store settings — the operator host
holds only the bearer token (ADR-0089 decision 5). Enforced by ``test_no_service_import``.

``read_cached_token`` is a module-local stub here so this module is self-contained until
the ``kdivectl login`` token cache (M2.2/2, issue #250) replaces it with a real reader.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastmcp import Client
from fastmcp.client.auth import BearerAuth

import kdive.config as config
from kdive.config.cli_settings import SERVER_URL, TOKEN


def read_cached_token() -> str | None:
    """Return the cached login token, or ``None`` when no cache exists.

    Stub until M2.2/2 (issue #250) supplies the ``0600`` login-cache reader; until then
    only the explicit ``KDIVE_TOKEN`` path yields a token.
    """
    return None


@dataclass(frozen=True)
class Session:
    """A resolved server URL plus the bearer token kdivectl presents."""

    url: str
    token: str

    @classmethod
    def from_env(cls) -> Session:
        """Resolve the server URL and token from config, failing closed without a token.

        Raises:
            SystemExit: When neither ``KDIVE_TOKEN`` nor the login cache yields a token.
        """
        token = config.get(TOKEN) or read_cached_token()
        if not token:
            raise SystemExit("no token: run `kdivectl login` or set KDIVE_TOKEN")
        url = config.get(SERVER_URL)
        if not url:
            raise SystemExit("no server URL: set KDIVE_SERVER_URL")
        return cls(url=url, token=token)

    def client(self) -> Client:
        """Build an authenticated MCP client bound to this session's URL and token."""
        return Client(self.url, auth=BearerAuth(self.token))
