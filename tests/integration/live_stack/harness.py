"""MCP-over-HTTP wire harness + OIDC token issuance (ADR-0044).

A test-side seam imported by the live-stack spine driver (issue #100):

* :func:`mint_token` obtains a bearer token from the mock-oauth2-server by driving its
  interactive-login authorization-code flow and posting a literal ``claims`` JSON object;
  the returned access token carries the nested-object ``roles`` claim and the
  ``platform_roles`` array claim (proven to flow into the access token, ADR-0044 Context).
* :class:`LiveStackClient` wraps :class:`fastmcp.Client`, parsing each tool result's
  structured output back into the project :class:`~kdive.mcp.responses.ToolResponse`.

This module imports no pytest symbols so it stays importable as a plain library.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Self

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from kdive.mcp.responses import ToolResponse

_DEFAULT_AUDIENCE = "kdive"
_DEFAULT_CLIENT_ID = "kdive-test"
_REDIRECT_URI = "http://localhost:1234/callback"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class LiveStackToolError(RuntimeError):
    """A tool call returned an error result over the wire (e.g. a raised authz denial).

    fastmcp surfaces a handler that *raises* (rather than returning a :class:`ToolResponse`)
    as a tool-error ``CallToolResult`` (``is_error`` true, no ``structured_content``). The
    driver asserts the RBAC raised-path on this typed error rather than on an
    ``error_category`` (ADR-0045 §2).
    """

    def __init__(self, tool: str, message: str) -> None:
        self.tool = tool
        self.message = message
        super().__init__(f"tool {tool!r} returned an error: {message}")


def _tool_error_text(result: object) -> str:
    """Best-effort human-readable text from a tool-error ``CallToolResult``."""
    content = getattr(result, "content", None)
    if content:
        first = content[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            return text
    return "tool error"


@dataclass(frozen=True)
class OidcIssuer:
    """The mock-OIDC issuer the harness mints tokens from (ADR-0044).

    ``base_url`` is the per-issuer base (e.g. ``http://localhost:8090/default``); the
    token/authorize/jwks endpoints derive from it. Built from the ``KDIVE_OIDC_*`` env the
    server also reads, or constructed directly in a test.
    """

    base_url: str
    audience: str = _DEFAULT_AUDIENCE
    client_id: str = _DEFAULT_CLIENT_ID

    @classmethod
    def from_env(cls) -> Self:
        """Resolve the issuer from ``KDIVE_OIDC_*``; raise if the base URL is unset."""
        base_url = os.environ.get("KDIVE_OIDC_ISSUER")
        if not base_url:
            raise RuntimeError("KDIVE_OIDC_ISSUER is not set; cannot reach the mock-OIDC issuer")
        return cls(
            base_url=base_url,
            audience=os.environ.get("KDIVE_OIDC_AUDIENCE", _DEFAULT_AUDIENCE),
            client_id=os.environ.get("KDIVE_OIDC_CLIENT_ID", _DEFAULT_CLIENT_ID),
        )

    @property
    def authorize_endpoint(self) -> str:
        """The OAuth authorization endpoint (drives the login form)."""
        return f"{self.base_url}/authorize"

    @property
    def token_endpoint(self) -> str:
        """The OAuth token endpoint (exchanges the code for an access token)."""
        return f"{self.base_url}/token"

    @property
    def jwks_uri(self) -> str:
        """The JWKS endpoint the verifier reads to validate signatures."""
        return f"{self.base_url}/jwks"


def _build_claims(
    *,
    subject: str,
    audience: str,
    projects: Sequence[str],
    roles: Mapping[str, str],
    platform_roles: Sequence[str] | None,
    agent_session: str | None,
) -> dict[str, object]:
    """Build the literal ``claims`` JSON the login form carries.

    ``roles`` becomes the nested-object claim ``{project: role}``; ``platform_roles``,
    when not ``None``, becomes the flat array claim. ``None`` for ``platform_roles`` or
    ``agent_session`` omits that claim entirely (distinct from an empty list).
    """
    claims: dict[str, object] = {
        "sub": subject,
        "aud": audience,
        "projects": list(projects),
        "roles": dict(roles),
    }
    if platform_roles is not None:
        claims["platform_roles"] = list(platform_roles)
    if agent_session is not None:
        claims["agent_session"] = agent_session
    return claims


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Suppress redirect-following so the authorization ``code`` is read from Location.

    Returning ``None`` from :meth:`redirect_request` makes urllib raise an ``HTTPError``
    carrying the 302 and its ``Location`` header instead of following the redirect.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def _authorization_code(issuer: OidcIssuer, claims: Mapping[str, object]) -> str:
    """Drive the login form and return the authorization ``code`` from the 302 redirect."""
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": issuer.client_id,
            "redirect_uri": _REDIRECT_URI,
            "scope": "openid",
            "state": "kdive-harness",
        }
    )
    authorize_url = f"{issuer.authorize_endpoint}?{params}"
    # The login form has no action: it posts back to the authorize URL (oauth params on
    # the query string) with username + a literal claims JSON.
    body = urllib.parse.urlencode(
        {"username": str(claims["sub"]), "claims": json.dumps(claims)}
    ).encode()
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(authorize_url, data=body, method="POST")
    try:
        opener.open(request)
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location")
        if exc.code in _REDIRECT_STATUSES and location:
            code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query)).get("code")
            if code:
                return code
        raise RuntimeError(
            f"issuer login did not return an authorization code (status {exc.code})"
        ) from exc
    raise RuntimeError("issuer login did not redirect with an authorization code")


def _exchange_code(issuer: OidcIssuer, code: str) -> str:
    """Exchange the authorization ``code`` for the access token."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "client_id": issuer.client_id,
        }
    ).encode()
    request = urllib.request.Request(issuer.token_endpoint, data=body, method="POST")
    with urllib.request.urlopen(request) as response:
        payload = json.loads(response.read())
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("issuer token response carried no access_token")
    return access_token


def mint_token(
    issuer: OidcIssuer,
    *,
    subject: str,
    projects: Sequence[str],
    roles: Mapping[str, str],
    platform_roles: Sequence[str] | None = None,
    agent_session: str | None = None,
) -> str:
    """Mint an access token from the mock-OIDC issuer carrying the kdive claims.

    Drives the issuer's interactive-login authorization-code flow: POST the login form
    with a literal ``claims`` JSON (nested-object ``roles`` + optional ``platform_roles``
    array), capture the ``code`` from the redirect, exchange it for the access token. The
    token validates through the server's real ``JWTVerifier`` (ADR-0044).
    """
    claims = _build_claims(
        subject=subject,
        audience=issuer.audience,
        projects=projects,
        roles=roles,
        platform_roles=platform_roles,
        agent_session=agent_session,
    )
    code = _authorization_code(issuer, claims)
    return _exchange_code(issuer, code)


class LiveStackClient:
    """A thin wrapper over :class:`fastmcp.Client` returning parsed envelopes (ADR-0044).

    ``call_tool`` parses ``CallToolResult.structured_content`` — a clean ``dict`` — back into
    the project :class:`ToolResponse`: a scalar tool's payload is the object dict, a
    ``list[ToolResponse]`` tool is wrapped as ``{"result": [...]}``. The constructor accepts an
    already-built client (the in-memory tier injects one over a probe app); :meth:`over_http`
    builds the streamable-HTTP + bearer client for the live tier.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    @classmethod
    def over_http(cls, base_url: str, token: str) -> Self:
        """Build a streamable-HTTP client carrying ``token`` as the bearer."""
        transport = StreamableHttpTransport(
            url=base_url, headers={"Authorization": f"Bearer {token}"}
        )
        return cls(Client(transport))

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.__aexit__(*exc)

    async def list_tools(self) -> list[str]:
        """Return the registered tool names."""
        tools = await self._client.list_tools()
        return [tool.name for tool in tools]

    async def call_tool(self, name: str, **args: object) -> ToolResponse | list[ToolResponse]:
        """Call ``name`` and parse the structured output into ``ToolResponse``.

        Reads ``CallToolResult.structured_content`` — a clean ``dict`` (fastmcp 3.4.0). A
        ``list[ToolResponse]`` tool is wrapped by FastMCP as ``{"result": [<dict>, ...]}``,
        so a payload that is exactly a single ``result`` key holding a list parses to a list
        of envelopes; any other object is one envelope. ``CallToolResult.data`` is not used:
        it is a FastMCP-generated plain class (``Root``), not a pydantic model, so it has no
        ``model_dump``.

        A tool-error result (``is_error`` true — a handler that *raised*, e.g. an authz denial
        that surfaces as a raise rather than a ``ToolResponse``) raises
        :class:`LiveStackToolError` before the structured-content parse (ADR-0045 §2).

        ``raise_on_error=False`` is required: fastmcp's ``Client.call_tool`` otherwise raises
        its own ``fastmcp.exceptions.ToolError`` on an error result, defeating the typed
        ``LiveStackToolError`` wrapping the driver asserts on. Passing it returns the
        ``CallToolResult`` so the ``is_error`` branch below can re-raise the typed error.
        """
        result = await self._client.call_tool(name, args, raise_on_error=False)
        if getattr(result, "is_error", False):
            raise LiveStackToolError(name, _tool_error_text(result))
        payload = result.structured_content
        if payload is None:
            raise RuntimeError(f"tool {name!r} returned no structured content")
        inner = payload.get("result")
        if list(payload) == ["result"] and isinstance(inner, list):
            return [ToolResponse.model_validate(item) for item in inner]
        return ToolResponse.model_validate(payload)
