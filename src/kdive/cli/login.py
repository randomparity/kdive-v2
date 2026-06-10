"""``kdivectl login``: acquire a bearer token (mock-OIDC) and cache it 0600 (ADR-0089).

The token is written to a per-user file created 0600 (parent 0700) and is never logged or
printed. Production operators bring their own token via ``KDIVE_TOKEN``; this verb targets
dev/CI and the boundary test, parameterized on the *platform-role* axis the CLI acts on
(``platform_admin`` / ``platform_operator`` / none) — not the project ``Role`` triad.

The mock-OIDC authorization-code flow (``OidcIssuer``, ``_authorization_code``,
``_exchange_code``, ``_build_claims``) lives here so it is the single source of truth; the
live-stack wire harness imports these symbols rather than re-declaring them.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import kdive.config as config
from kdive.config.cli_settings import CLI_CLIENT_ID

_DEFAULT_AUDIENCE = "kdive"
_DEFAULT_CLIENT_ID = "kdive-test"
_REDIRECT_URI = "http://localhost:1234/callback"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _cache_path() -> Path:
    """Return the per-user token-cache path (``$XDG_STATE_HOME/kdive/token``)."""
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "kdive" / "token"


def write_cached_token(token: str) -> None:
    """Write ``token`` to the cache as a 0600 file under a 0700 parent.

    The file is opened with ``O_CREAT`` mode ``0o600`` and re-tightened with ``chmod`` so a
    pre-existing file or a widened ``umask`` cannot leave the token group/world-readable.
    The token value is never logged.
    """
    path = _cache_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
    os.chmod(path, 0o600)


def read_cached_token() -> str | None:
    """Return the cached token, or ``None`` when the cache is absent or empty."""
    try:
        return _cache_path().read_text().strip() or None
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class OidcIssuer:
    """The mock-OIDC issuer ``kdivectl`` mints tokens from (ADR-0044/0089).

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
    client_id: str | None = None,
) -> dict[str, object]:
    """Build the literal ``claims`` JSON the login form carries.

    ``roles`` becomes the nested-object claim ``{project: role}``; ``platform_roles``,
    when not ``None``, becomes the flat array claim. ``None`` for ``platform_roles`` or
    ``agent_session`` omits that claim entirely (distinct from an empty list). ``client_id``,
    when set, becomes the OIDC ``azp`` claim the server's actor map resolves to
    ``operator-cli``.
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
    if client_id is not None:
        claims["azp"] = client_id
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


def login(platform_role: str | None) -> str:
    """Mint a bearer token on the platform-role axis and cache it 0600.

    Drives the mock-OIDC authorization-code flow against the ``KDIVE_OIDC_*`` issuer,
    building claims with ``platform_roles=[platform_role]`` when set (omitted when ``None``)
    and ``azp`` from ``KDIVE_CLI_CLIENT_ID``. On success the token is written to the 0600
    cache and returned; the token itself is never logged or printed.

    Args:
        platform_role: ``platform_admin``/``platform_operator`` to encode as the sole
            ``platform_roles`` entry, or ``None`` to omit the claim.

    Returns:
        The minted access token.
    """
    issuer = OidcIssuer.from_env()
    platform_roles = [platform_role] if platform_role is not None else None
    claims = _build_claims(
        subject="operator-cli",
        audience=issuer.audience,
        projects=[],
        roles={},
        platform_roles=platform_roles,
        agent_session=None,
        client_id=config.get(CLI_CLIENT_ID),
    )
    code = _authorization_code(issuer, claims)
    token = _exchange_code(issuer, code)
    write_cached_token(token)
    return token
