# MCP-over-HTTP wire harness + OIDC token issuance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable test-side `fastmcp.Client` wrapper (`LiveStackClient`) and an OIDC token-issuance helper (`mint_token`) that mints `viewer`/`operator`/`admin` + `platform_auditor` tokens from the mock-oauth2-server, plus a three-tier wire smoke test, so issue #100's spine driver can import the harness and drive tools over HTTP under real tokens.

**Architecture:** Test-only code under `tests/integration/live_stack/`. `mint_token` drives the issuer's interactive-login authorization-code flow (login-form `claims` → access token, proven to carry the nested `roles` object + `platform_roles` array — see ADR-0044). `LiveStackClient` wraps `fastmcp.Client`, parsing `CallToolResult.structured_content` (a clean dict; list tools wrapped as `{"result": [...]}`) into the project `ToolResponse`. The smoke test runs in three tiers: an always-run in-memory tier (rides the repo's disposable-Postgres gating), an `oidc_issuer`-gated tier (the standing claim-shape gate), and a `live_stack`-gated tier (full HTTP). No `src/` changes.

**Tech Stack:** Python 3.13, `fastmcp` 3.4.0 (`Client`, `StreamableHttpTransport`, `JWTVerifier`, `RSAKeyPair`), `httpx` 0.28, `psycopg_pool.AsyncConnectionPool`, `pytest`, the repo's `migrated_url` disposable-Postgres fixture.

**Decisions:** [ADR-0044](../../adr/0044-mcp-wire-harness-oidc-token-issuance.md) · **Spec:** [`../specs/2026-06-04-mcp-wire-harness-oidc-design.md`](../specs/2026-06-04-mcp-wire-harness-oidc-design.md) · **Issue:** #98

---

## File structure

```
tests/integration/__init__.py               # NEW (if absent) — package marker
tests/integration/live_stack/__init__.py     # NEW — package marker
tests/integration/live_stack/harness.py      # NEW — OidcIssuer, _build_claims, mint_token, LiveStackClient
tests/integration/live_stack/conftest.py     # NEW — preflight skip helpers + fixtures for the smoke tiers
tests/integration/test_wire_harness.py        # NEW — three-tier smoke test
pyproject.toml                                # MODIFY — register `oidc_issuer` + `live_stack` markers
```

Responsibilities: `harness.py` is the importable seam (no pytest imports — issue #100 imports it). `conftest.py` and `test_wire_harness.py` hold the skip logic + the three tiers. The marker registration is the only non-test file touched. (`docker-compose.yml` was already fixed to `3.0.3` in this branch.)

---

## Task 1: Register the pytest markers

**Files:**
- Modify: `pyproject.toml` (the `[tool.pytest.ini_options]` `markers` list)

- [ ] **Step 1: Add the two markers**

In `pyproject.toml`, change the `markers` list under `[tool.pytest.ini_options]` from:

```toml
markers = [
  "live_vm: requires an operator-provided libvirt/KVM environment (KVM/nested-virt host, libvirt, kdump-enabled guest image)",
]
```

to:

```toml
markers = [
  "live_vm: requires an operator-provided libvirt/KVM environment (KVM/nested-virt host, libvirt, kdump-enabled guest image)",
  "oidc_issuer: requires only the mock-oauth2-server container up (no kdive server, no Postgres, no VM)",
  "live_stack: requires a running kdive server + issuer + Postgres (operator-run, skipped on pull_request)",
]
```

- [ ] **Step 2: Verify markers are registered (no warning)**

Run: `cd /home/dave/src/kdive-worktrees/mcp-http-harness-98 && uv run python -m pytest --markers 2>&1 | grep -E 'oidc_issuer|live_stack'`
Expected: both marker descriptions print; no "unknown marker" warnings.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "test: register oidc_issuer and live_stack pytest markers"
```

---

## Task 2: Package markers + `_build_claims` (pure, network-free)

**Files:**
- Create: `tests/integration/__init__.py` (only if it does not already exist — empty file)
- Create: `tests/integration/live_stack/__init__.py` (empty file)
- Create: `tests/integration/live_stack/harness.py`
- Test: `tests/integration/live_stack/test_harness_unit.py`

- [ ] **Step 1: Create the package markers**

```bash
cd /home/dave/src/kdive-worktrees/mcp-http-harness-98
test -f tests/integration/__init__.py || : > tests/integration/__init__.py
mkdir -p tests/integration/live_stack
: > tests/integration/live_stack/__init__.py
```

- [ ] **Step 2: Write the failing unit test for `_build_claims`**

Create `tests/integration/live_stack/test_harness_unit.py`:

```python
"""Unit tests for the network-free parts of the wire harness (the claim builder)."""

from __future__ import annotations

from tests.integration.live_stack.harness import _build_claims


def test_build_claims_nested_roles_object() -> None:
    claims = _build_claims(
        subject="admin-proj-a",
        audience="kdive",
        projects=["proj-a"],
        roles={"proj-a": "admin"},
        platform_roles=None,
        agent_session="sess-1",
    )
    assert claims["sub"] == "admin-proj-a"
    assert claims["aud"] == "kdive"
    assert claims["projects"] == ["proj-a"]
    assert claims["roles"] == {"proj-a": "admin"}  # nested object, not flat
    assert claims["agent_session"] == "sess-1"
    assert "platform_roles" not in claims  # None -> omitted


def test_build_claims_platform_roles_array() -> None:
    claims = _build_claims(
        subject="auditor",
        audience="kdive",
        projects=["proj-a"],
        roles={"proj-a": "viewer"},
        platform_roles=["platform_auditor"],
        agent_session=None,
    )
    assert claims["platform_roles"] == ["platform_auditor"]  # flat array
    assert "agent_session" not in claims  # None -> omitted


def test_build_claims_empty_platform_roles_is_present_but_empty() -> None:
    claims = _build_claims(
        subject="x",
        audience="kdive",
        projects=[],
        roles={},
        platform_roles=[],
        agent_session=None,
    )
    assert claims["platform_roles"] == []  # [] -> present-but-empty, distinct from None
    assert claims["projects"] == []
    assert claims["roles"] == {}
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run python -m pytest tests/integration/live_stack/test_harness_unit.py -q`
Expected: FAIL — `ModuleNotFoundError` / `ImportError: cannot import name '_build_claims'`.

- [ ] **Step 4: Write the harness module skeleton + `_build_claims`**

Create `tests/integration/live_stack/harness.py`:

```python
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
```

- [ ] **Step 5: Run to verify the unit tests pass**

Run: `uv run python -m pytest tests/integration/live_stack/test_harness_unit.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add tests/integration/__init__.py tests/integration/live_stack/__init__.py \
  tests/integration/live_stack/harness.py tests/integration/live_stack/test_harness_unit.py
git commit -m "test: add wire-harness package and _build_claims with unit tests"
```

---

## Task 3: `OidcIssuer` config (env-resolved, derived endpoints)

**Files:**
- Modify: `tests/integration/live_stack/harness.py`
- Test: `tests/integration/live_stack/test_harness_unit.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/live_stack/test_harness_unit.py`:

```python
import pytest

from tests.integration.live_stack.harness import OidcIssuer


def test_oidc_issuer_derived_endpoints() -> None:
    issuer = OidcIssuer(
        base_url="http://localhost:8090/default", audience="kdive", client_id="kdive-test"
    )
    assert issuer.authorize_endpoint == "http://localhost:8090/default/authorize"
    assert issuer.token_endpoint == "http://localhost:8090/default/token"
    assert issuer.jwks_uri == "http://localhost:8090/default/jwks"


def test_oidc_issuer_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_OIDC_ISSUER", "http://localhost:8090/default")
    monkeypatch.setenv("KDIVE_OIDC_AUDIENCE", "kdive")
    monkeypatch.delenv("KDIVE_OIDC_CLIENT_ID", raising=False)
    issuer = OidcIssuer.from_env()
    assert issuer.base_url == "http://localhost:8090/default"
    assert issuer.audience == "kdive"
    assert issuer.client_id == "kdive-test"  # default


def test_oidc_issuer_from_env_missing_base_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_OIDC_ISSUER", raising=False)
    with pytest.raises(RuntimeError, match="KDIVE_OIDC_ISSUER"):
        OidcIssuer.from_env()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/integration/live_stack/test_harness_unit.py -q`
Expected: FAIL — `ImportError: cannot import name 'OidcIssuer'`.

- [ ] **Step 3: Add `OidcIssuer` to `harness.py`**

Insert after the constants block (before `_build_claims`):

```python
@dataclass(frozen=True)
class OidcIssuer:
    """The mock-OIDC issuer the harness mints tokens from (ADR-0044).

    ``base_url`` is the per-issuer base (e.g. ``http://localhost:8090/default``); the
    token/authorize/jwks endpoints derive from it. Built from the ``KDIVE_OIDC_*`` env
    the server also reads, or constructed directly in a test.
    """

    base_url: str
    audience: str = _DEFAULT_AUDIENCE
    client_id: str = _DEFAULT_CLIENT_ID

    @classmethod
    def from_env(cls) -> Self:
        """Resolve the issuer from ``KDIVE_OIDC_*``; raise if the base URL is unset."""
        base_url = os.environ.get("KDIVE_OIDC_ISSUER")
        if not base_url:
            raise RuntimeError(
                "KDIVE_OIDC_ISSUER is not set; cannot reach the mock-OIDC issuer"
            )
        return cls(
            base_url=base_url,
            audience=os.environ.get("KDIVE_OIDC_AUDIENCE", _DEFAULT_AUDIENCE),
            client_id=os.environ.get("KDIVE_OIDC_CLIENT_ID", _DEFAULT_CLIENT_ID),
        )

    @property
    def authorize_endpoint(self) -> str:
        return f"{self.base_url}/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.base_url}/token"

    @property
    def jwks_uri(self) -> str:
        return f"{self.base_url}/jwks"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/integration/live_stack/test_harness_unit.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/integration/live_stack/harness.py tests/integration/live_stack/test_harness_unit.py
git commit -m "test: add OidcIssuer config with env resolution and derived endpoints"
```

---

## Task 4: `mint_token` (the login-form authorization-code flow)

**Files:**
- Modify: `tests/integration/live_stack/harness.py`

This is the proven flow (ADR-0044 Context). No unit test here — it needs the live issuer; it is exercised by the `oidc_issuer` tier in Task 7. Implement it now so the harness is complete.

- [ ] **Step 1: Add `mint_token` and its private flow helpers to `harness.py`**

Append:

```python
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Suppress redirect-following so the authorization ``code`` is read from Location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _authorization_code(issuer: OidcIssuer, claims: Mapping[str, object]) -> str:
    """Drive the login form and return the authorization ``code`` from the 302."""
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
        if exc.code in (301, 302, 303, 307, 308) and location:
            code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query)).get(
                "code"
            )
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
    with urllib.request.urlopen(request) as response:  # noqa: S310 (localhost test issuer)
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
    array), capture the ``code`` from the redirect, exchange it for the access token.
    The token validates through the server's real ``JWTVerifier`` (ADR-0044).
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
```

- [ ] **Step 2: Verify the module imports cleanly and `mint_token` is exported**

Run: `uv run python -c "from tests.integration.live_stack.harness import mint_token, OidcIssuer; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/live_stack/harness.py
git commit -m "test: add mint_token issuer login-form authorization-code flow"
```

---

## Task 5: `LiveStackClient` envelope parsing (pinned to fastmcp 3.4.0)

**Files:**
- Modify: `tests/integration/live_stack/harness.py`
- Test: `tests/integration/live_stack/test_client_inmemory.py`

- [ ] **Step 1: Write the failing test (in-memory probe app, no DB, no auth)**

The repo has **no async-test plugin** (no `pytest-asyncio`/`anyio`); tests drive coroutines
with `asyncio.run(...)` exactly as `tests/mcp/test_app.py` does. Follow that. The probe app's
tools do **not** read auth (the in-memory transport carries no token — verified).

Create `tests/integration/live_stack/test_client_inmemory.py`:

```python
"""In-memory LiveStackClient tests: envelope parsing + the .data shape pin (no DB, no auth)."""

from __future__ import annotations

import asyncio

from fastmcp import Client, FastMCP

from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.harness import LiveStackClient


def _probe_app() -> FastMCP:
    app: FastMCP = FastMCP(name="probe")

    @app.tool(name="scalar.one")
    def scalar_one() -> ToolResponse:
        return ToolResponse.success("obj-1", "ok", suggested_next_actions=["next"])

    @app.tool(name="list.many")
    def list_many() -> list[ToolResponse]:
        return [ToolResponse.success("a", "ok"), ToolResponse.success("b", "ok")]

    return app


def test_call_tool_scalar_returns_one_envelope() -> None:
    async def _run() -> None:
        client = LiveStackClient(Client(_probe_app()))
        async with client:
            result = await client.call_tool("scalar.one")
        assert isinstance(result, ToolResponse)
        assert result.object_id == "obj-1"
        assert result.status == "ok"

    asyncio.run(_run())


def test_call_tool_list_returns_envelope_list() -> None:
    async def _run() -> None:
        client = LiveStackClient(Client(_probe_app()))
        async with client:
            result = await client.call_tool("list.many")
        assert isinstance(result, list)
        assert [r.object_id for r in result] == ["a", "b"]
        assert all(isinstance(r, ToolResponse) for r in result)

    asyncio.run(_run())


def test_list_tools_returns_names() -> None:
    async def _run() -> None:
        client = LiveStackClient(Client(_probe_app()))
        async with client:
            names = await client.list_tools()
        assert {"scalar.one", "list.many"} <= set(names)

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/integration/live_stack/test_client_inmemory.py -q`
Expected: FAIL — `ImportError: cannot import name 'LiveStackClient'`.

- [ ] **Step 3: Add `LiveStackClient` to `harness.py`**

Append:

```python
class LiveStackClient:
    """A thin wrapper over :class:`fastmcp.Client` returning parsed envelopes (ADR-0044).

    ``call_tool`` parses ``CallToolResult.data`` — already deserialized by FastMCP into a
    pydantic model (scalar tool) or a list of them (a ``list[ToolResponse]`` tool) — back
    into the project :class:`ToolResponse`. The constructor accepts an already-built
    client (the in-memory tier injects one over ``build_app``); :meth:`over_http` builds
    the streamable-HTTP + bearer client for the live tier.
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

    async def call_tool(
        self, name: str, **args: object
    ) -> ToolResponse | list[ToolResponse]:
        """Call ``name`` and parse the structured output into ``ToolResponse``.

        Reads ``CallToolResult.structured_content`` — a clean dict. A ``list[ToolResponse]``
        tool is wrapped as ``{"result": [<dict>, ...]}``; any other object is one envelope.
        ``.data`` is not used (it is a FastMCP-generated plain ``Root`` class, no
        ``model_dump``).
        """
        result = await self._client.call_tool(name, args)
        payload = result.structured_content
        if payload is None:
            raise RuntimeError(f"tool {name!r} returned no structured content")
        inner = payload.get("result")
        if list(payload) == ["result"] and isinstance(inner, list):
            return [ToolResponse.model_validate(item) for item in inner]
        return ToolResponse.model_validate(payload)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/integration/live_stack/test_client_inmemory.py -q`
Expected: PASS (3 passed). If the async marker errors, apply the `asyncio.run` fallback from Step 1's note and re-run.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/live_stack/harness.py tests/integration/live_stack/test_client_inmemory.py
git commit -m "test: add LiveStackClient with pinned fastmcp .data envelope parsing"
```

---

## Task 6: Preflight skip helpers + the in-memory claim-shape tier

**Files:**
- Create: `tests/integration/live_stack/conftest.py`
- Create: `tests/integration/test_wire_harness.py`

The in-memory tier does **not** drive a kdive plane tool: the in-memory `FastMCPTransport`
carries no access token (verified — it rejects `auth=` and `get_access_token()` returns
`None`), so any tool calling `current_context()` (including `resources.list`) cannot run that
way. The per-role `resources.list` probe lives in the `live_stack` tier (Task 8). Here the
in-memory tier covers only the claim *shape* (the envelope seam is already covered in Task 5).

- [ ] **Step 1: Write the skip-helper conftest**

Create `tests/integration/live_stack/conftest.py`:

```python
"""Preflight helpers for the wire-harness smoke tiers (the ADR-0035 §4 skip idiom)."""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

from tests.integration.live_stack.harness import OidcIssuer


def _issuer_reachable(issuer: OidcIssuer) -> bool:
    try:
        with urllib.request.urlopen(issuer.jwks_uri, timeout=5) as response:  # noqa: S310
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def require_issuer() -> OidcIssuer:
    """Skip unless the mock-OIDC issuer is configured and its JWKS is reachable."""
    base_url = os.environ.get("KDIVE_OIDC_ISSUER")
    if not base_url:
        pytest.skip("KDIVE_OIDC_ISSUER unset; start the issuer (`docker compose up -d oidc`)")
    issuer = OidcIssuer.from_env()
    if not _issuer_reachable(issuer):
        pytest.skip(f"mock-OIDC issuer JWKS unreachable at {issuer.jwks_uri}")
    return issuer


def require_stack() -> str:
    """Skip unless a kdive server base URL is configured (the live_stack tier)."""
    base_url = os.environ.get("KDIVE_STACK_BASE_URL")
    if not base_url:
        pytest.skip("KDIVE_STACK_BASE_URL unset; bring up the stack (see the live-stack runbook)")
    return base_url
```

- [ ] **Step 2: Write the in-memory claim-shape tier (failing — module not present)**

Create `tests/integration/test_wire_harness.py`:

```python
"""Three-tier wire smoke test (ADR-0044): in-memory / oidc_issuer / live_stack.

The in-memory tier (Docker-free) covers the claim shape; the issuer-only and live tiers gate
on their backing service via markers + a preflight skip.
"""

from __future__ import annotations

import asyncio

import jwt  # PyJWT, used to decode a token's claims without verifying the signature

from fastmcp.server.auth.providers.jwt import JWTVerifier
from kdive.security.rbac import Role, roles_from_claims
from tests.integration.live_stack.harness import (
    LiveStackClient,
    _build_claims,
    mint_token,
)
from tests.mcp.conftest import AUDIENCE, make_keypair, mint

_PROJECT = "proj-a"
# (subject, roles map, platform_roles) per role token the smoke exercises.
_ROLE_SUBJECTS = (
    ("viewer-proj-a", {_PROJECT: "viewer"}, None),
    ("operator-proj-a", {_PROJECT: "operator"}, None),
    ("admin-proj-a", {_PROJECT: "admin"}, None),
    ("auditor", {_PROJECT: "viewer"}, ["platform_auditor"]),
)


def test_inmemory_tier_claim_shapes_round_trip() -> None:
    """_build_claims + the in-process mint produce the nested roles + platform_roles shapes."""
    keypair = make_keypair()
    token = mint(keypair, subject="auditor", projects=[_PROJECT], roles={_PROJECT: "viewer"})
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["roles"] == {_PROJECT: "viewer"}  # nested object survives the JWT

    claims = _build_claims(
        subject="auditor",
        audience=AUDIENCE,
        projects=[_PROJECT],
        roles={_PROJECT: "viewer"},
        platform_roles=["platform_auditor"],
        agent_session=None,
    )
    assert claims["platform_roles"] == ["platform_auditor"]  # flat array
    assert claims["roles"] == {_PROJECT: "viewer"}  # nested object
```

- [ ] **Step 3: Run to verify it passes**

Run: `uv run python -m pytest tests/integration/test_wire_harness.py -q`
Expected: PASS (1 passed) — no Docker, no DB, no issuer needed. (The `LiveStackClient`,
`mint_token`, `JWTVerifier`, `Role`, `roles_from_claims`, `require_issuer`/`require_stack`
imports are used by the later tiers added in Tasks 7-8; if `ruff` flags any as unused before
those tasks land, add the later-task tests in the same working session so the imports are used,
or temporarily add them in Task 7/8 instead. Cleanest: write Tasks 6-8 together, then run lint
once at Task 9.)

- [ ] **Step 4: Commit**

```bash
git add tests/integration/live_stack/conftest.py tests/integration/test_wire_harness.py
git commit -m "test: add in-memory claim-shape tier + preflight skip helpers"
```

---

## Task 7: The `oidc_issuer` gate tier (the standing claim-shape regression)

**Files:**
- Modify: `tests/integration/test_wire_harness.py`

- [ ] **Step 1: Add the issuer-only gate test**

Append to `tests/integration/test_wire_harness.py`:

```python
import pytest

from tests.integration.live_stack.conftest import require_issuer


@pytest.mark.oidc_issuer
def test_oidc_issuer_tier_mints_and_verifies_claim_shapes() -> None:
    """The gate (ADR-0044): the issuer mints nested roles + platform_roles into the
    access token; the real JWTVerifier accepts; roles_from_claims parses; wrong-aud rejects."""
    issuer = require_issuer()

    async def _run() -> None:
        verifier = JWTVerifier(
            jwks_uri=issuer.jwks_uri, issuer=issuer.base_url, audience=issuer.audience
        )
        wrong_aud = JWTVerifier(
            jwks_uri=issuer.jwks_uri, issuer=issuer.base_url, audience="not-kdive"
        )
        for subject, roles, platform_roles in _ROLE_SUBJECTS:
            token = mint_token(
                issuer,
                subject=subject,
                projects=[_PROJECT],
                roles=roles,
                platform_roles=platform_roles,
                agent_session="sess-1",
            )
            verified = await verifier.verify_token(token)
            assert verified is not None, f"real verifier rejected {subject}'s token"
            assert verified.claims["roles"] == roles  # nested object survived
            parsed = roles_from_claims(verified.claims)
            assert parsed == {p: Role(r) for p, r in roles.items()}
            if platform_roles is not None:
                assert verified.claims["platform_roles"] == platform_roles  # flat array
            assert await wrong_aud.verify_token(token) is None  # verifier enforces aud

    asyncio.run(_run())
```

- [ ] **Step 2: Run with the issuer up (the gate)**

```bash
docker compose up -d oidc
sleep 4
KDIVE_OIDC_ISSUER=http://localhost:8090/default KDIVE_OIDC_AUDIENCE=kdive \
  uv run python -m pytest tests/integration/test_wire_harness.py -m oidc_issuer -q
```
Expected: PASS (1 passed) — the standing proof that nested `roles` + `platform_roles` reach the access token and validate through the real verifier.

- [ ] **Step 3: Run without the issuer to confirm a clean skip**

Run: `uv run python -m pytest tests/integration/test_wire_harness.py -m oidc_issuer -q` (no `KDIVE_OIDC_ISSUER`)
Expected: SKIPPED with the "start the issuer (`docker compose up -d oidc`)" reason. No error.

- [ ] **Step 4: Tear down the issuer**

Run: `docker compose down`

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_wire_harness.py
git commit -m "test: add oidc_issuer gate proving the claim shapes through the real verifier"
```

---

## Task 8: The `live_stack` wire tier (HTTP against a host-run server)

**Files:**
- Modify: `tests/integration/test_wire_harness.py`

- [ ] **Step 1: Add the live-stack wire test**

Append to `tests/integration/test_wire_harness.py`:

```python
from tests.integration.live_stack.conftest import require_stack


@pytest.mark.live_stack
def test_live_stack_tier_reads_resources_over_http_per_role() -> None:
    """Over HTTP against a host-run server: list_tools + a resources.list per role,
    tokens minted by the real issuer and validated through the server's verifier."""
    issuer = require_issuer()
    base_url = require_stack()

    async def _run() -> None:
        for subject, roles, platform_roles in _ROLE_SUBJECTS:
            token = mint_token(
                issuer,
                subject=subject,
                projects=[_PROJECT],
                roles=roles,
                platform_roles=platform_roles,
                agent_session="sess-1",
            )
            client = LiveStackClient.over_http(base_url, token)
            async with client:
                names = await client.list_tools()
                assert "resources.list" in names
                result = await client.call_tool("resources.list")
            assert isinstance(result, list)

    asyncio.run(_run())
```

- [ ] **Step 2: Confirm a clean skip without the stack**

Run: `uv run python -m pytest tests/integration/test_wire_harness.py -m live_stack -q`
Expected: SKIPPED with the "KDIVE_STACK_BASE_URL unset" reason (or the issuer-unset reason if that env is absent first). No error.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_wire_harness.py
git commit -m "test: add live_stack wire tier driving resources.list over HTTP"
```

---

## Task 9: Full guardrails + default-suite behavior

**Files:** none (verification only)

- [ ] **Step 1: Lint**

Run: `just lint`
Expected: clean (ruff check + format check pass). Fix any finding and re-run.

- [ ] **Step 2: Type-check (whole tree)**

Run: `just type`
Expected: `ty check` passes. Common fixes: the `_NoRedirect.redirect_request` override needs the `# noqa: ANN001` already present, or add precise param types if `ty` prefers them; ensure `Self` is imported from `typing`.

- [ ] **Step 2a: Resolve the urllib S310 lint if ruff flags it**

`urllib.request.urlopen` on a non-constant URL trips `S310` (only if the security ruleset is on). The localhost test issuer is trusted; keep the inline `# noqa: S310` comments already placed, or — if the repo's ruff set (`E,F,I,UP,B,SIM`) does not include `S` — remove the unneeded `# noqa` to avoid an `RUF100` unused-noqa error. Decide by reading the `[tool.ruff]` `select` in `pyproject.toml` and adjust.

- [ ] **Step 3: Run the default test suite (no markers)**

Run: `just test`
Expected: the in-memory tier + unit tests run (or SKIP if Docker absent); the `oidc_issuer` and `live_stack` tests are NOT selected by `just test` only if the recipe deselects them. Check the `just test` recipe: if it does not already exclude the new markers, confirm they SKIP cleanly (their preflight skips without the backing service) so the default suite stays green either way.

- [ ] **Step 4: Confirm importability by issue #100**

Run: `uv run python -c "from tests.integration.live_stack.harness import LiveStackClient, mint_token, OidcIssuer; print('importable by #100')"`
Expected: `importable by #100`.

- [ ] **Step 5: Commit any guardrail fixes**

```bash
git add -A
git commit -m "test: satisfy lint/type guardrails for the wire harness"
```

(Skip the commit if Steps 1-4 required no changes.)

---

## Self-review (spec coverage)

- `LiveStackClient` wrapper returning parsed envelopes, `.data` contract pinned → Task 5; ADR-0044 §3. ✓
- `mint_token` nested-object `roles` + array `platform_roles` via the login-form flow → Task 4; ADR-0044 §1/§2. ✓
- `OidcIssuer` env-resolved config → Task 3. ✓
- In-memory tier (Docker-free; claim-shape + envelope seam, no auth'd tool calls) → Tasks 5/6. ✓
- `oidc_issuer` gate tier (real issuer + real `JWTVerifier` + `roles_from_claims` + wrong-aud reject) → Task 7; ADR-0044 §3. ✓
- `live_stack` wire tier over HTTP — the per-role `resources.list` probe lives here (real auth) → Task 8. ✓
- `oidc_issuer` + `live_stack` markers, distinct from `live_vm` → Task 1; ADR-0044 §4. ✓
- Clean skips when issuer/stack absent → Tasks 7/8 skip steps. ✓
- Importable by issue #100 → Task 9 Step 4. ✓
- Edges: omit-vs-empty `platform_roles`, missing-env `OidcIssuer.from_env`, scalar-vs-list `.data` → Tasks 2/3/5. ✓
- No `src/` change → all tasks are test-only + the marker registration. ✓
