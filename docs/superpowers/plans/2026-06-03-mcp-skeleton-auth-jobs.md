# MCP/HTTP skeleton + OIDC auth + jobs.* tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the M0 MCP server skeleton — a FastMCP streamable-HTTP app that authenticates bearer JWTs, resolves a `(principal, agent_session, project)` context, exposes the four `jobs.*` tools over the durable queue, and ships `server`/`worker` entrypoints.

**Architecture:** A uniform `ToolResponse` envelope (ADR-0019) returned by every tool; an `auth.py` that builds FastMCP's `JWTVerifier` and reads the verified token's claims; `tools/jobs.py` with plain async handlers wrapped as FastMCP tools and a `register(app, pool)` hook; `app.py` assembling the app via two symmetric plane seams (tools + handlers); `__main__.py` with `server`/`worker` subcommands. Handlers take their dependencies as arguments so they are unit-tested directly, never through MCP transport.

**Tech Stack:** Python 3.13 · `uv` · FastMCP 3.4.0 (`JWTVerifier`, `RSAKeyPair`) · psycopg 3 async · Pydantic v2 · pytest (sync tests wrapping `asyncio.run`) · testcontainers Postgres.

**Design reference:** [`docs/superpowers/specs/2026-06-03-mcp-skeleton-auth-jobs-design.md`](../specs/2026-06-03-mcp-skeleton-auth-jobs-design.md) · ADR-0010, ADR-0006, ADR-0002, ADR-0019, ADR-0014.

**Conventions to honor (verified against the repo):**
- Async tests are **sync functions** wrapping `asyncio.run(_run())` (see `tests/jobs/test_queue.py`). No pytest-asyncio.
- DB tests depend on the `migrated_url` fixture from `tests/db/conftest.py`; they skip when Docker is unavailable. A `tests/mcp/conftest.py` re-exports the DB fixtures.
- Config reads `os.environ.get(...)` and raises `CategorizedError(category=ErrorCategory.CONFIGURATION_ERROR)` on a missing value (see `src/kdive/db/pool.py`).
- `JOBS` repo (`src/kdive/db/repositories.py`) gives `JOBS.get(conn, id)` and `JOBS.update_state(conn, id, state)`. `enqueue` lives in `src/kdive/jobs/queue.py`.
- Run guardrails after each implementation step: `uv run ruff check`, `uv run ruff format`, `uv run ty check src`, `uv run python -m pytest -q`.

---

## File Structure

| Path | Responsibility |
|------|----------------|
| `src/kdive/mcp/responses.py` | `ToolResponse` envelope + `from_job` (ADR-0019). |
| `src/kdive/mcp/auth.py` | `RequestContext`, `AuthError`, `build_verifier`, `context_from_claims`, `current_context`, `require_project`. |
| `src/kdive/jobs/queue.py` (modify) | Add `recent_jobs(conn, limit)` read. |
| `src/kdive/mcp/tools/jobs.py` | `get_job`/`wait_job`/`cancel_job`/`list_jobs` handlers, error mapping, `register(app, pool)`. |
| `src/kdive/mcp/app.py` | `build_app`, `build_handler_registry`, the two registrar-seam tuples. |
| `src/kdive/__main__.py` | `server`/`worker` argparse CLI. |
| `tests/mcp/__init__.py`, `tests/mcp/conftest.py` | Test package + fixture re-export + a token-minting helper. |
| `tests/mcp/test_responses.py` | Envelope + `from_job` + invariant tests (no DB). |
| `tests/mcp/test_auth.py` | Verifier iss/aud/expiry, claims passthrough, context derivation. |
| `tests/mcp/test_jobs_tools.py` | The four handlers against a migrated DB. |
| `tests/mcp/test_app.py` | App assembly: tool registration, injected verifier, handler seam. |
| `tests/mcp/test_main.py` | CLI argument parsing. |

---

## Task 1: `ToolResponse` envelope

**Files:**
- Create: `src/kdive/mcp/responses.py`
- Test: `tests/mcp/test_responses.py`
- Create (support): `tests/mcp/__init__.py`

- [ ] **Step 1: Create the test package marker**

Create `tests/mcp/__init__.py` (empty file).

- [ ] **Step 2: Write the failing tests**

Create `tests/mcp/test_responses.py`:

```python
"""ToolResponse envelope tests (ADR-0019) — pure, no DB."""

from __future__ import annotations

from uuid import uuid4

import pytest

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Job
from kdive.domain.state import JobState
from kdive.mcp.responses import ToolResponse


def _job(state: JobState, *, result_ref: str | None = None,
         error_category: ErrorCategory | None = None) -> Job:
    return Job(
        id=uuid4(),
        kind="build",
        payload={},
        state=state,
        max_attempts=3,
        result_ref=result_ref,
        error_category=error_category,
        authorizing={"principal": "p"},
        dedup_key=str(uuid4()),
    )


def test_from_job_running_has_no_refs_and_polling_actions() -> None:
    job = _job(JobState.RUNNING)
    resp = ToolResponse.from_job(job)
    assert resp.object_id == str(job.id)
    assert resp.status == "running"
    assert resp.data == {"kind": "build"}
    assert resp.refs == {}
    assert resp.error_category is None
    assert resp.suggested_next_actions == ["jobs.wait", "jobs.cancel"]


def test_from_job_succeeded_exposes_result_ref() -> None:
    job = _job(JobState.SUCCEEDED, result_ref="tenant/run/abc/kernel")
    resp = ToolResponse.from_job(job)
    assert resp.status == "succeeded"
    assert resp.refs == {"result": "tenant/run/abc/kernel"}
    assert resp.suggested_next_actions == ["jobs.get"]


def test_from_job_failed_carries_category() -> None:
    job = _job(JobState.FAILED, error_category=ErrorCategory.BUILD_FAILURE)
    resp = ToolResponse.from_job(job)
    assert resp.status == "failed"
    assert resp.error_category == "build_failure"
    assert resp.suggested_next_actions == ["jobs.get"]


def test_from_job_canceled_has_no_actions() -> None:
    resp = ToolResponse.from_job(_job(JobState.CANCELED))
    assert resp.status == "canceled"
    assert resp.suggested_next_actions == []


def test_category_without_failure_is_rejected() -> None:
    with pytest.raises(ValueError, match="error_category"):
        ToolResponse(object_id="x", status="running", error_category="build_failure")


def test_failure_without_category_is_rejected() -> None:
    # The validator treats status in {"failed", "error"} as a failure status, which
    # therefore requires a category.
    with pytest.raises(ValueError, match="error_category"):
        ToolResponse(object_id="x", status="error", error_category=None)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_responses.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.mcp.responses'`.

- [ ] **Step 4: Implement `responses.py`**

Create `src/kdive/mcp/responses.py`:

```python
"""The uniform tool-response envelope every MCP tool returns (ADR-0019).

Every tool — across all planes — returns a :class:`ToolResponse` carrying the
object id, a status, literal next tool names, artifact references, and (only for a
failure) an error category. The shape is fixed surface-wide so an agent learns one
envelope and one polling pattern, and so "references, never log dumps" is structural
rather than per-plane discipline.
"""

from __future__ import annotations

from pydantic import BaseModel, model_validator

from kdive.domain.models import Job
from kdive.domain.state import JobState

# Literal next tool names by terminality of the job's state. Only `jobs.*` exist in
# M0; the artifact-retrieval action joins the succeeded row when `artifacts.get`
# ships (#19). See the design doc's suggested_next_actions table.
_NEXT_ACTIONS: dict[JobState, list[str]] = {
    JobState.QUEUED: ["jobs.wait", "jobs.cancel"],
    JobState.RUNNING: ["jobs.wait", "jobs.cancel"],
    JobState.SUCCEEDED: ["jobs.get"],
    JobState.FAILED: ["jobs.get"],
    JobState.CANCELED: [],
}

_FAILED_STATUS = JobState.FAILED.value


class ToolResponse(BaseModel):
    """The structured JSON every MCP tool returns (ADR-0019)."""

    object_id: str
    status: str
    suggested_next_actions: list[str] = []
    refs: dict[str, str] = {}
    error_category: str | None = None
    data: dict[str, str] = {}

    @model_validator(mode="after")
    def _category_iff_failed(self) -> ToolResponse:
        """Enforce: ``error_category`` is set iff the object is in a failure status.

        A ``failed`` status without a category, or any other status carrying one, is
        a producer bug — fail fast at construction (ADR-0019).
        """
        is_failed = self.status in (_FAILED_STATUS, "error")
        if is_failed and self.error_category is None:
            raise ValueError(f"status {self.status!r} requires an error_category")
        if not is_failed and self.error_category is not None:
            raise ValueError(
                f"error_category set on non-failure status {self.status!r}"
            )
        return self

    @classmethod
    def from_job(cls, job: Job) -> ToolResponse:
        """Build the job-handle envelope from a :class:`Job` row."""
        refs = {"result": job.result_ref} if job.result_ref else {}
        return cls(
            object_id=str(job.id),
            status=job.state.value,
            suggested_next_actions=list(_NEXT_ACTIONS[job.state]),
            refs=refs,
            error_category=job.error_category.value if job.error_category else None,
            data={"kind": job.kind.value},
        )
```

The validator trigger (`status in {"failed", "error"}`) matches the two validator
tests written in Step 2 — no test changes are needed here.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_responses.py -q`
Expected: PASS (6 tests).

- [ ] **Step 6: Guardrails + commit**

```bash
uv run ruff check src/kdive/mcp/responses.py tests/mcp/test_responses.py
uv run ruff format src/kdive/mcp tests/mcp
uv run ty check src
git add src/kdive/mcp/responses.py tests/mcp/__init__.py tests/mcp/test_responses.py
git commit -m "feat(mcp): tool-response envelope with job-handle mapping (ADR-0019)"
```

---

## Task 2: `auth.py` — verifier, context, failure contract

**Files:**
- Create: `src/kdive/mcp/auth.py`
- Test: `tests/mcp/test_auth.py`
- Create (support): `tests/mcp/conftest.py`

- [ ] **Step 1: Write the token-minting fixture/helper**

Create `tests/mcp/conftest.py`:

```python
"""MCP test fixtures: re-export DB fixtures and a JWT-minting helper."""

from __future__ import annotations

from fastmcp.server.auth.providers.jwt import RSAKeyPair

# Re-export the disposable-Postgres fixtures so DB-backed MCP tests can use them.
from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401

ISSUER = "https://idp.test.kdive"
AUDIENCE = "kdive"


def make_keypair() -> RSAKeyPair:
    return RSAKeyPair.generate()


def mint(
    keypair: RSAKeyPair,
    *,
    subject: str = "user-1",
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    agent_session: str | None = "sess-1",
    projects: list[str] | None = None,
    expires_in_seconds: int = 3600,
) -> str:
    """Mint a signed JWT carrying the kdive custom claims."""
    extra: dict[str, object] = {}
    if agent_session is not None:
        extra["agent_session"] = agent_session
    if projects is not None:
        extra["projects"] = projects
    return keypair.create_token(
        subject=subject,
        issuer=issuer,
        audience=audience,
        additional_claims=extra,
        expires_in_seconds=expires_in_seconds,
    )
```

> Verify `create_token`'s parameter names against fastmcp 3.4.0 before relying on them: `python -c "from fastmcp.server.auth.providers.jwt import RSAKeyPair; import inspect; print(inspect.signature(RSAKeyPair.create_token))"`. Adjust `expires_in_seconds`/`additional_claims` names if the signature differs.

- [ ] **Step 2: Write the failing tests**

Create `tests/mcp/test_auth.py`:

```python
"""auth.py: verifier enforcement + context derivation."""

from __future__ import annotations

import asyncio

import pytest

from kdive.domain.errors import CategorizedError
from kdive.mcp.auth import (
    AuthError,
    RequestContext,
    build_verifier,
    context_from_claims,
    require_project,
)
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair, mint


def test_verifier_accepts_valid_and_rejects_iss_aud_expiry() -> None:
    kp = make_keypair()
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)

    async def _run() -> None:
        good = await verifier.verify_token(mint(kp))
        assert good is not None
        assert good.claims["sub"] == "user-1"
        # subject is NOT populated by JWTVerifier — must read claims["sub"].
        assert good.subject is None
        assert await verifier.verify_token(mint(kp, issuer="https://evil")) is None
        assert await verifier.verify_token(mint(kp, audience="other")) is None
        assert await verifier.verify_token(mint(kp, expires_in_seconds=-10)) is None

    asyncio.run(_run())


def test_context_from_claims_full() -> None:
    ctx = context_from_claims(
        {"sub": "user-9", "agent_session": "sess-x", "projects": ["a", "b"]}
    )
    assert ctx == RequestContext(principal="user-9", agent_session="sess-x",
                                 projects=("a", "b"))


def test_context_from_claims_optional_fields_absent() -> None:
    ctx = context_from_claims({"sub": "user-9"})
    assert ctx.agent_session is None
    assert ctx.projects == ()


def test_context_from_claims_missing_subject_raises() -> None:
    with pytest.raises(AuthError):
        context_from_claims({"agent_session": "x"})


def test_require_project_validates_membership() -> None:
    ctx = RequestContext(principal="p", agent_session=None, projects=("a", "b"))
    assert require_project(ctx, "a") == "a"
    with pytest.raises(AuthError):
        require_project(ctx, "c")


def test_build_verifier_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_OIDC_JWKS_URI", raising=False)
    monkeypatch.setenv("KDIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("KDIVE_OIDC_AUDIENCE", AUDIENCE)
    with pytest.raises(CategorizedError, match="KDIVE_OIDC_JWKS_URI"):
        build_verifier()


def test_build_verifier_constructs_with_full_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_OIDC_JWKS_URI", "https://idp.test/jwks")
    monkeypatch.setenv("KDIVE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("KDIVE_OIDC_AUDIENCE", AUDIENCE)
    verifier = build_verifier()
    assert verifier.issuer == ISSUER
    assert verifier.audience == AUDIENCE
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_auth.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.mcp.auth'`.

- [ ] **Step 4: Implement `auth.py`**

Create `src/kdive/mcp/auth.py`:

```python
"""Bearer-JWT verification and the request-context accessor (ADR-0010, ADR-0006).

`build_verifier` constructs FastMCP's `JWTVerifier` from the OIDC env vars; it
enforces `iss` and `aud` natively (ADR-0002). `context_from_claims` turns a verified
token's claims into the `(principal, agent_session, project)` tuple every tool reads
for attribution. `current_context` is the FastMCP-facing accessor; `require_project`
validates a requested project against the token's granted set (its first callers are
the plane tools, not `jobs.*`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token

from kdive.domain.errors import CategorizedError, ErrorCategory

_JWKS_URI_ENV = "KDIVE_OIDC_JWKS_URI"
_ISSUER_ENV = "KDIVE_OIDC_ISSUER"
_AUDIENCE_ENV = "KDIVE_OIDC_AUDIENCE"


class AuthError(Exception):
    """A verified transport carried claims that cannot authorize the request.

    Distinct from transport-level rejection (a missing/invalid/expired bearer is a
    401 from FastMCP's middleware before any tool runs). Raised when the verified
    token lacks a usable subject, or a requested project is not granted.
    """


@dataclass(frozen=True)
class RequestContext:
    """The `(principal, agent_session, project)` attribution tuple (ADR-0006)."""

    principal: str
    agent_session: str | None
    projects: tuple[str, ...]


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CategorizedError(
            f"{name} is not set; cannot verify bearer tokens",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return value


def build_verifier() -> JWTVerifier:
    """Build the `JWTVerifier` from the OIDC env vars, enforcing `iss` + `aud`."""
    return JWTVerifier(
        jwks_uri=_require_env(_JWKS_URI_ENV),
        issuer=_require_env(_ISSUER_ENV),
        audience=_require_env(_AUDIENCE_ENV),
    )


def context_from_claims(claims: Mapping[str, object]) -> RequestContext:
    """Derive the request context from a verified token's claims.

    Reads the principal from ``claims["sub"]`` (FastMCP leaves
    ``AccessToken.subject`` unset). ``agent_session`` is optional in M0; ``projects``
    defaults to an empty tuple.

    Raises:
        AuthError: The token carries no usable ``sub``.
    """
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AuthError("verified token has no usable subject (sub) claim")
    agent_session = claims.get("agent_session")
    if agent_session is not None and not isinstance(agent_session, str):
        raise AuthError("agent_session claim is not a string")
    raw_projects = claims.get("projects") or ()
    if not isinstance(raw_projects, (list, tuple)):
        raise AuthError("projects claim is not a list")
    projects = tuple(str(p) for p in raw_projects)
    return RequestContext(principal=subject, agent_session=agent_session,
                          projects=projects)


def current_context() -> RequestContext:
    """Read the context from the in-flight request's verified token.

    Raises:
        AuthError: No verified token reached the tool (defense in depth; the auth
            middleware should already have returned 401).
    """
    token = get_access_token()
    if token is None:
        raise AuthError("no authenticated token in the request context")
    return context_from_claims(token.claims)


def require_project(ctx: RequestContext, project: str) -> str:
    """Validate ``project`` is granted to ``ctx``; return it, or raise.

    Raises:
        AuthError: ``project`` is not in the token's ``projects`` claim.
    """
    if project not in ctx.projects:
        raise AuthError(f"project {project!r} is not granted to {ctx.principal!r}")
    return project
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_auth.py -q`
Expected: PASS. If a DB container is required by an imported fixture but Docker is absent, these specific tests still run (they do not request `migrated_url`).

- [ ] **Step 6: Guardrails + commit**

```bash
uv run ruff check src/kdive/mcp/auth.py tests/mcp/test_auth.py tests/mcp/conftest.py
uv run ruff format src/kdive/mcp tests/mcp
uv run ty check src
git add src/kdive/mcp/auth.py tests/mcp/test_auth.py tests/mcp/conftest.py
git commit -m "feat(mcp): JWT verifier + (principal, agent_session, project) context"
```

---

## Task 3: `recent_jobs` queue read

**Files:**
- Modify: `src/kdive/jobs/queue.py` (append a function)
- Test: `tests/jobs/test_queue.py` (append a test) — follows the existing file's idiom

- [ ] **Step 1: Write the failing test**

Append to `tests/jobs/test_queue.py` (match the file's existing imports/idiom; it already imports `asyncio`, `psycopg`, and `queue`):

```python
def test_recent_jobs_newest_first_and_capped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            for i in range(3):
                await queue.enqueue(conn, "build", {"i": i}, {"principal": "p"}, f"d{i}")
            recent = await queue.recent_jobs(conn, limit=2)
        assert len(recent) == 2
        # newest-first: the last-enqueued dedup_key appears first
        assert recent[0].dedup_key == "d2"
        assert recent[1].dedup_key == "d1"

    asyncio.run(_run())


def test_recent_jobs_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            assert await queue.recent_jobs(conn, limit=10) == []

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/jobs/test_queue.py -k recent_jobs -q`
Expected: FAIL with `AttributeError: module 'kdive.jobs.queue' has no attribute 'recent_jobs'` (or SKIP if Docker is unavailable — if skipped, note it and proceed; CI runs it).

- [ ] **Step 3: Implement `recent_jobs`**

Append to `src/kdive/jobs/queue.py`:

```python
async def recent_jobs(conn: AsyncConnection, limit: int) -> list[Job]:
    """Return the most recently created jobs, newest first, capped at ``limit``.

    The ``id`` tiebreaker makes the order total when two jobs share a ``created_at``
    microsecond, so the cap never drops an arbitrary one of a tied pair. M0 is
    single-project with no per-principal scoping on this read (see #10 design); a
    ``state``/``kind``/``project`` filter arrives with RBAC (#11).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT %s",
            (limit,),
        )
        rows = await cur.fetchall()
    return [Job.model_validate(row) for row in rows]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/jobs/test_queue.py -k recent_jobs -q`
Expected: PASS (or SKIP without Docker).

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check src/kdive/jobs/queue.py tests/jobs/test_queue.py
uv run ruff format src/kdive/jobs/queue.py tests/jobs/test_queue.py
uv run ty check src
git add src/kdive/jobs/queue.py tests/jobs/test_queue.py
git commit -m "feat(jobs): recent_jobs read for the jobs.list tool"
```

---

## Task 4: `tools/jobs.py` — handlers + register

**Files:**
- Create: `src/kdive/mcp/tools/jobs.py`
- Test: `tests/mcp/test_jobs_tools.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_jobs_tools.py`:

```python
"""jobs.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.state import JobState
from kdive.jobs import queue
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import jobs as jobs_tools

CTX = RequestContext(principal="user-1", agent_session="s", projects=("proj",))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _enqueue(pool: AsyncConnectionPool, dedup: str) -> str:
    async with pool.connection() as conn:
        job = await queue.enqueue(conn, "build", {}, {"principal": "p"}, dedup)
    return str(job.id)


def test_get_known_job_returns_status(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            resp = await jobs_tools.get_job(pool, CTX, job_id)
        assert resp.object_id == job_id
        assert resp.status == "queued"
        assert resp.data == {"kind": "build"}

    asyncio.run(_run())


def test_get_unknown_job_is_error_envelope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.get_job(pool, CTX, str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_cancel_queued_job_transitions(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            resp = await jobs_tools.cancel_job(pool, CTX, job_id)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_cancel_terminal_job_is_error_envelope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            await jobs_tools.cancel_job(pool, CTX, job_id)  # -> canceled (terminal)
            resp = await jobs_tools.cancel_job(pool, CTX, job_id)  # again
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_wait_returns_immediately_for_terminal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            await jobs_tools.cancel_job(pool, CTX, job_id)
            resp = await jobs_tools.wait_job(pool, CTX, job_id, timeout_s=5.0)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_wait_zero_timeout_is_single_read(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")  # stays queued (no worker)
            resp = await jobs_tools.wait_job(pool, CTX, job_id, timeout_s=0.0)
        assert resp.status == "queued"  # one read, no wait

    asyncio.run(_run())


def test_wait_loops_until_terminal(migrated_url: str) -> None:
    """Exercise the sleep-then-re-poll branch: a concurrent task cancels the job
    after one poll interval, and wait must return the canceled envelope having
    looped at least once (timeout long enough to require a real poll)."""
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")

            async def _cancel_after_delay() -> None:
                await asyncio.sleep(jobs_tools.POLL_INTERVAL_S + 0.1)
                await jobs_tools.cancel_job(pool, CTX, job_id)

            canceller = asyncio.create_task(_cancel_after_delay())
            resp = await jobs_tools.wait_job(pool, CTX, job_id, timeout_s=5.0)
            await canceller
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_list_jobs_newest_first_and_capped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(3):
                await _enqueue(pool, f"d{i}")
            resp = await jobs_tools.list_jobs(pool, CTX, limit=2)
        assert len(resp) == 2
        assert all(r.status == "queued" for r in resp)

    asyncio.run(_run())


def test_list_jobs_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.list_jobs(pool, CTX, limit=50)
        assert resp == []

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/test_jobs_tools.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.mcp.tools.jobs'` (or SKIP without Docker).

- [ ] **Step 3: Implement `tools/jobs.py`**

Create `src/kdive/mcp/tools/jobs.py`:

```python
"""The `jobs.*` MCP tools over the durable queue (#10).

Each tool is a thin FastMCP wrapper over a plain async handler that takes its
dependencies (the pool, the request context) as arguments, so handlers are tested
directly without MCP transport. A handler that raises a domain error becomes an
error `ToolResponse` (with the most specific `ErrorCategory`), never an unhandled
500. M0 does not scope these by principal/project — see the #10 design's isolation
posture; #11 (RBAC) adds scoping.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import JOBS, ObjectNotFound
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.state import IllegalTransition, JobState
from kdive.jobs import queue
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse

_log = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.5
MAX_WAIT_S = 300.0
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200

_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED}


def _error(object_id: str, category: ErrorCategory) -> ToolResponse:
    return ToolResponse(object_id=object_id, status="error",
                        error_category=category.value)


def _as_uuid(job_id: str) -> UUID | None:
    try:
        return UUID(job_id)
    except ValueError:
        return None


async def get_job(pool: AsyncConnectionPool, ctx: RequestContext,
                  job_id: str) -> ToolResponse:
    """Return the job's handle envelope, or an error envelope if absent/malformed."""
    uid = _as_uuid(job_id)
    if uid is None:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    async with pool.connection() as conn:
        job = await JOBS.get(conn, uid)
    if job is None:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    return ToolResponse.from_job(job)


async def wait_job(pool: AsyncConnectionPool, ctx: RequestContext, job_id: str,
                   timeout_s: float) -> ToolResponse:
    """Poll until the job is terminal or ``timeout_s`` (clamped) elapses.

    Each poll acquires and releases a pool connection (holds none while sleeping).
    A non-positive timeout means a single read.
    """
    uid = _as_uuid(job_id)
    if uid is None:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    deadline = asyncio.get_running_loop().time() + min(max(timeout_s, 0.0), MAX_WAIT_S)
    while True:
        async with pool.connection() as conn:
            job = await JOBS.get(conn, uid)
        if job is None:
            return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
        if job.state in _TERMINAL or asyncio.get_running_loop().time() >= deadline:
            return ToolResponse.from_job(job)
        await asyncio.sleep(POLL_INTERVAL_S)


async def cancel_job(pool: AsyncConnectionPool, ctx: RequestContext,
                     job_id: str) -> ToolResponse:
    """Transition the job to ``canceled`` (cooperative); error on a terminal job."""
    uid = _as_uuid(job_id)
    if uid is None:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    try:
        async with pool.connection() as conn:
            job = await JOBS.update_state(conn, uid, JobState.CANCELED)
    except ObjectNotFound:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    except IllegalTransition:
        return _error(job_id, ErrorCategory.CONFIGURATION_ERROR)
    return ToolResponse.from_job(job)


async def list_jobs(pool: AsyncConnectionPool, ctx: RequestContext, *,
                    limit: int) -> list[ToolResponse]:
    """Return the newest jobs (capped), each as an envelope, isolating bad rows."""
    capped = max(1, min(limit, MAX_LIST_LIMIT))
    async with pool.connection() as conn:
        jobs = await queue.recent_jobs(conn, capped)
    responses: list[ToolResponse] = []
    for job in jobs:
        try:
            responses.append(ToolResponse.from_job(job))
        except ValueError:
            _log.warning("job %s violates the response invariant; degraded", job.id)
            responses.append(_error(str(job.id), ErrorCategory.INFRASTRUCTURE_FAILURE))
    return responses


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the four `jobs.*` tools on ``app``, bound to ``pool``."""

    @app.tool(name="jobs.get")
    async def jobs_get(job_id: str) -> ToolResponse:
        ctx = current_context()
        with bind_context(principal=ctx.principal, job_id=job_id):
            return await get_job(pool, ctx, job_id)

    @app.tool(name="jobs.wait")
    async def jobs_wait(job_id: str, timeout_s: float = 30.0) -> ToolResponse:
        ctx = current_context()
        with bind_context(principal=ctx.principal, job_id=job_id):
            return await wait_job(pool, ctx, job_id, timeout_s)

    @app.tool(name="jobs.cancel")
    async def jobs_cancel(job_id: str) -> ToolResponse:
        ctx = current_context()
        with bind_context(principal=ctx.principal, job_id=job_id):
            return await cancel_job(pool, ctx, job_id)

    @app.tool(name="jobs.list")
    async def jobs_list(limit: int = DEFAULT_LIST_LIMIT) -> list[ToolResponse]:
        ctx = current_context()
        with bind_context(principal=ctx.principal):
            return await list_jobs(pool, ctx, limit=limit)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/test_jobs_tools.py -q`
Expected: PASS (9 tests) or SKIP without Docker.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check src/kdive/mcp/tools/jobs.py tests/mcp/test_jobs_tools.py
uv run ruff format src/kdive/mcp tests/mcp
uv run ty check src
git add src/kdive/mcp/tools/jobs.py tests/mcp/test_jobs_tools.py
git commit -m "feat(mcp): jobs.get/.wait/.cancel/.list handlers and register hook"
```

---

## Task 5: `app.py` — assembly + two plane seams

**Files:**
- Create: `src/kdive/mcp/app.py`
- Test: `tests/mcp/test_app.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_app.py`:

```python
"""app.py: tool registration via the seam, with an injected verifier."""

from __future__ import annotations

import asyncio

from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.jobs.models import HandlerRegistry
from kdive.mcp.app import build_app, build_handler_registry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


def _verifier() -> JWTVerifier:
    kp = make_keypair()
    return JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)


def test_build_app_registers_jobs_tools() -> None:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier())

    async def _run() -> None:
        # Verified against fastmcp 3.4.0: FastMCP.list_tools() is async and returns
        # list[Tool], each with a .name (there is no get_tools()).
        tools = await app.list_tools()
        names = {t.name for t in tools}
        assert {"jobs.get", "jobs.wait", "jobs.cancel", "jobs.list"} <= names

    asyncio.run(_run())


def test_build_handler_registry_is_empty_in_m0() -> None:
    registry = build_handler_registry()
    assert isinstance(registry, HandlerRegistry)
    # No real handlers in M0; an unknown kind has no handler.
    assert registry.get("build") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/test_app.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'kdive.mcp.app'`.

- [ ] **Step 3: Implement `app.py`**

Create `src/kdive/mcp/app.py`:

```python
"""FastMCP application assembly and the two plane registrar seams (#10).

A plane issue (#11+) ships a tool surface *and* a job handler. The skeleton exposes
two symmetric seams so a plane is added by appending to a tuple here and never edits
the entrypoint: `_PLANE_REGISTRARS` (tools) and `_HANDLER_REGISTRARS` (worker job
handlers). Both are empty of non-jobs planes in M0.
"""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.jobs.models import HandlerRegistry
from kdive.mcp.auth import build_verifier
from kdive.mcp.tools import jobs

# Tool seam: each plane exposes register(app, pool); build_app calls them all.
_PLANE_REGISTRARS: tuple[Callable[[FastMCP, AsyncConnectionPool], None], ...] = (
    jobs.register,
)

# Handler seam: each plane exposes register_handlers(registry); the worker calls
# them all. jobs.* register no JobHandler (they are read/cancel tools, not kinds),
# so M0 has no entries — the seam exists for the plane issues.
_HANDLER_REGISTRARS: tuple[Callable[[HandlerRegistry], None], ...] = ()


def build_app(pool: AsyncConnectionPool, *,
              verifier: JWTVerifier | None = None) -> FastMCP:
    """Construct the FastMCP app and register every plane's tools.

    Args:
        pool: The shared async connection pool tools read through.
        verifier: An injected verifier (tests pass a local-keypair one); when
            ``None``, built from the OIDC env vars via :func:`build_verifier`.
    """
    app: FastMCP = FastMCP(name="kdive", auth=verifier or build_verifier())
    for register in _PLANE_REGISTRARS:
        register(app, pool)
    return app


def build_handler_registry() -> HandlerRegistry:
    """Build the worker's `HandlerRegistry` from the handler seam (empty in M0)."""
    registry = HandlerRegistry()
    for register in _HANDLER_REGISTRARS:
        register(registry)
    return registry
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/test_app.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
uv run ruff check src/kdive/mcp/app.py tests/mcp/test_app.py
uv run ruff format src/kdive/mcp tests/mcp
uv run ty check src
git add src/kdive/mcp/app.py tests/mcp/test_app.py
git commit -m "feat(mcp): app assembly with tool + handler plane seams"
```

---

## Task 6: `__main__.py` — server/worker CLI

**Files:**
- Create: `src/kdive/__main__.py`
- Test: `tests/mcp/test_main.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_main.py`:

```python
"""CLI argument parsing for `python -m kdive`."""

from __future__ import annotations

import pytest

from kdive.__main__ import build_parser


def test_server_subcommand_parses() -> None:
    args = build_parser().parse_args(["server"])
    assert args.command == "server"
    assert args.log_level == "INFO"


def test_worker_subcommand_parses_with_log_level() -> None:
    args = build_parser().parse_args(["--log-level", "DEBUG", "worker"])
    assert args.command == "worker"
    assert args.log_level == "DEBUG"


def test_no_subcommand_errors() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/test_main.py -q`
Expected: FAIL with `ModuleNotFoundError` or `ImportError: cannot import name 'build_parser'`.

- [ ] **Step 3: Implement `__main__.py`**

Create `src/kdive/__main__.py`:

```python
"""Process entrypoints: `python -m kdive server|worker` (#10).

`server` runs the FastMCP streamable-HTTP app; `worker` runs the job-queue worker
loop. Both configure the structured logger first (ADR-0014). The `reconciler`
subcommand is added by #12; the parser is structured so it slots in.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket

from kdive.db.pool import create_pool
from kdive.log import configure_logging

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with the `server`/`worker` subcommands."""
    parser = argparse.ArgumentParser(prog="kdive")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("KDIVE_LOG_LEVEL", "INFO"),
        help="structured-logging level (default INFO)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("server", help="run the MCP streamable-HTTP server")
    sub.add_parser("worker", help="run the job-queue worker loop")
    return parser


async def _run_server(host: str, port: int) -> None:
    from kdive.mcp.app import build_app

    pool = create_pool()
    await pool.open()
    try:
        app = build_app(pool)
        await app.run_async(transport="http", host=host, port=port)
    finally:
        await pool.close()


async def _run_worker() -> None:
    from kdive.jobs.worker import Worker
    from kdive.mcp.app import build_handler_registry

    pool = create_pool(min_size=2, max_size=4)
    await pool.open()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    try:
        worker = Worker(pool, build_handler_registry(), worker_id=worker_id)
        await worker.run(stop)
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> None:
    """Parse arguments, configure logging, and dispatch to the chosen subcommand."""
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    if args.command == "server":
        host = os.environ.get("KDIVE_HTTP_HOST", _DEFAULT_HOST)
        port = int(os.environ.get("KDIVE_HTTP_PORT", _DEFAULT_PORT))
        asyncio.run(_run_server(host, port))
    elif args.command == "worker":
        asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
```

> `create_pool` currently takes only `conninfo`. Step 4 widens it to accept `min_size`/`max_size` so the worker can satisfy the `pool.max_size >= 2` invariant. If you prefer not to touch `create_pool`, construct the worker pool inline with `AsyncConnectionPool(database_url(), min_size=2, max_size=4, open=False)` instead — but widening `create_pool` keeps one construction path.

- [ ] **Step 4: Widen `create_pool` for pool sizing**

In `src/kdive/db/pool.py`, change `create_pool` to accept optional sizing (keep the existing default behavior intact):

```python
def create_pool(
    conninfo: str | None = None,
    *,
    min_size: int = 1,
    max_size: int = 10,
) -> AsyncConnectionPool:
    """Build an unopened async connection pool.

    Args:
        conninfo: Connection string; defaults to ``database_url()`` (env).
        min_size: Minimum pooled connections kept open.
        max_size: Maximum concurrent connections (the worker needs >= 2).
    """
    return AsyncConnectionPool(
        conninfo or database_url(), min_size=min_size, max_size=max_size, open=False
    )
```

Confirm no existing caller breaks: `rg -n "create_pool\(" src tests`. The signature is backward-compatible (new params are keyword-only with defaults).

- [ ] **Step 5: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/test_main.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Guardrails + commit**

```bash
uv run ruff check src/kdive/__main__.py src/kdive/db/pool.py tests/mcp/test_main.py
uv run ruff format src/kdive tests/mcp
uv run ty check src
git add src/kdive/__main__.py src/kdive/db/pool.py tests/mcp/test_main.py
git commit -m "feat(mcp): server/worker entrypoints with structured logging"
```

---

## Task 7: Full-suite verification

- [ ] **Step 1: Run the whole suite**

Run: `uv run python -m pytest -q`
Expected: all pass; the MCP DB-backed tests pass when Docker is available, skip otherwise. No pre-existing test regresses.

- [ ] **Step 2: Final guardrail sweep**

```bash
uv run ruff check
uv run ruff format
uv run ty check src
```
Expected: clean (zero warnings). Fix anything before proceeding.

- [ ] **Step 3: Confirm the acceptance criteria map to tests**

- "no/invalid token is rejected" → `test_auth.py::test_verifier_accepts_valid_and_rejects_iss_aud_expiry` (verifier returns `None` for bad tokens; FastMCP 401s on `None`).
- "a valid token resolves the principal context" → `test_auth.py::test_context_from_claims_full` + the claims-passthrough assertion.
- "`jobs.get` on a known job returns its status" → `test_jobs_tools.py::test_get_known_job_returns_status`.
- "structured JSON shape matches the spec" → `test_responses.py` (object_id, status, suggested_next_actions, refs).

---

## Self-review notes (completed against the spec)

- **Spec coverage:** responses (Task 1), auth verifier+context+failure contract (Task 2), `recent_jobs` (Task 3), four handlers + error mapping + register (Task 4), two plane seams (Task 5), CLI + logging + pool sizing (Task 6). The `require_project` function ships in Task 2 and is unit-tested though `jobs.*` do not call it (spec: auth owns it for the plane tools).
- **Deferred-by-design (not gaps):** RBAC/authz scoping, audit log, redaction, reconciler, REST/gRPC — all named non-goals in the spec.
- **API-uncertainty callouts:** `RSAKeyPair.create_token` kwargs (Task 2 Step 1), `FastMCP` tool-introspection accessor (Task 5 Step 1), and `app.run_async` signature (Task 6) each carry a one-line verification command — confirm against fastmcp 3.4.0 before relying on them, since these are the points most likely to drift from this plan.
- **Type consistency:** handler signatures `(_pool, ctx, job_id|*, limit)` are identical across Task 4's implementation and tests; `ToolResponse` field names match between Task 1 and its consumers.
```
