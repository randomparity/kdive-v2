# MCP/HTTP skeleton + OIDC auth + jobs.* tools — Design

**Issue:** #10 (M0) · **Depends on:** #3 (structured logging — merged), #7
(repository layer / `JOBS` repo — merged), #9 (job queue & worker — merged) ·
**Decisions:** [ADR-0010](../../adr/0010-fastmcp-framework-auth.md) (FastMCP +
streamable-HTTP auth), [ADR-0006](../../adr/0006-oidc-rbac-attribution.md)
(`(principal, agent_session)` attribution), [ADR-0002](../../adr/0002-multi-user-mcp-http.md)
(`iss`+`aud` bearer model), [ADR-0019](../../adr/0019-tool-response-envelope.md)
(tool-response envelope) · **Parent spec:**
[`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md) ("MCP tool
surface", "Cross-cutting concerns")

## Goal

The MCP server skeleton for M0: a FastMCP application served over streamable HTTP
that authenticates bearer JWTs, resolves a `(principal, agent_session, project)`
request context, and exposes the four `jobs.*` tools over the existing durable job
queue. Plus the process entrypoint (`server` and `worker` subcommands). Four new
modules and one response model:

- `src/kdive/mcp/responses.py` — the `ToolResponse` envelope (ADR-0019).
- `src/kdive/mcp/auth.py` — the `JWTVerifier` factory and the request-context
  accessor.
- `src/kdive/mcp/tools/jobs.py` — the `jobs.get/.wait/.cancel/.list` handlers and a
  `register(app)` hook.
- `src/kdive/mcp/app.py` — `build_app()` constructing `FastMCP(name, auth)` and
  calling every plane's `register(app)`.
- `src/kdive/__main__.py` — the `server` / `worker` CLI.

This layer sits **above** the job queue/worker (#9) and repository layer (#7), and
**below** the plane handlers (#11+) that will register their own tools and job
handlers. It owns *how a request is authenticated and shaped into a response* and
*how the two long-running processes start*; it does not own *what a job does* (the
handler) or *role/destructive-op authorization* (#11, RBAC/gate).

## Non-goals

- **No RBAC / destructive-op gate.** `viewer`/`operator`/`admin` role checks and
  the three-check destructive gate are issue #11. This skeleton authenticates
  (proves who the caller is) and resolves the context tuple; it does **not**
  authorize a job read against the job's `authorizing` tuple or the caller's role.
  `jobs.*` are read/cancel tools available to any authenticated principal in M0.
- **No audit log.** The append-only `audit_log` write on every transition is #11.
  This issue emits structured **logs** (ADR-0014) per request/job, which are not the
  audit record.
- **No redaction port.** `src/kdive/security/redaction.py` lands with #23. The
  envelope (ADR-0019) carries artifact **references**, not bytes, so `jobs.*` have
  nothing to redact in M0 (a `result_ref` is an object-store key, not guest output).
  The redaction gate is named here as the future owner of "is this ref
  response-eligible", not implemented.
- **No human REST/gRPC surface.** ADR-0010 notes the same token validation backs a
  REST/gRPC surface through shared middleware "outside FastMCP". M0 ships only the
  MCP surface; the shared-middleware extraction is deferred.
- **No other planes.** Only `tools/jobs.py` registers here. The `register(app)`
  hook is the seam the plane issues use; `app.py` lists the registries it calls and
  a plane issue appends its module to that list.
- **No reconciler.** The `reconciler` subcommand is issue #12; `__main__.py` ships
  `server` and `worker` only and is structured so `reconciler` slots in later.

## Components

### `responses.py` — the envelope (ADR-0019)

```python
class ToolResponse(BaseModel):
    object_id: str
    status: str
    suggested_next_actions: list[str] = []
    refs: dict[str, str] = {}
    error_category: str | None = None
    data: dict[str, str] = {}

    @classmethod
    def from_job(cls, job: Job) -> ToolResponse: ...
```

`from_job` maps the `Job` to the envelope:

- `object_id = str(job.id)`
- `status = job.state.value`
- `data = {"kind": job.kind.value}`
- `refs = {"result": job.result_ref}` when `job.result_ref` is set, else `{}`
- `error_category = job.error_category.value` when set, else `None`
- `suggested_next_actions` derived from `job.state` (table below)

A model validator enforces the ADR-0019 "category iff failed" invariant:
`error_category` is non-`None` **iff** `status == JobState.FAILED`. A `Job` row in a
non-`failed` state with a stray `error_category`, or a `failed` row without one, is
a producer bug and raises `ValueError` at envelope construction (fail fast, ADR-0014
log carries the job id).

**`suggested_next_actions` by job state** (literal next tool names):

| state       | suggested_next_actions          | rationale                                  |
|-------------|---------------------------------|--------------------------------------------|
| `queued`    | `["jobs.wait", "jobs.cancel"]`  | not started; poll or abort                 |
| `running`   | `["jobs.wait", "jobs.cancel"]`  | in flight; poll or abort                   |
| `succeeded` | `["jobs.get"]`                  | terminal; re-read for `result_ref`         |
| `failed`    | `["jobs.get"]`                  | terminal; re-read for `error_category`     |
| `canceled`  | `[]`                            | terminal, nothing actionable               |

`jobs.*` are the only tool names this skeleton can name. A succeeded job's
`result_ref` points at an artifact, but the artifact-retrieval tool (`artifacts.get`)
ships with #19; suggesting a tool that does not exist yet would mislead an agent, so
M0 suggests `jobs.get` (always present) and the artifact-retrieval action is added to
the table when #19 lands. This is recorded so the omission is intentional, not
forgotten.

### `auth.py` — verifier + context

**Verifier factory.** `build_verifier() -> JWTVerifier` reads three env vars and
constructs FastMCP's `JWTVerifier(jwks_uri=…, issuer=…, audience=…)`:

| env var               | maps to            | required |
|-----------------------|--------------------|----------|
| `KDIVE_OIDC_JWKS_URI` | `jwks_uri`         | yes      |
| `KDIVE_OIDC_ISSUER`   | `issuer` (`iss`)   | yes      |
| `KDIVE_OIDC_AUDIENCE` | `audience` (`aud`) | yes      |

A missing or empty value raises `CategorizedError(CONFIGURATION_ERROR)` with the
offending var name (the established config pattern, `db/pool.py`). `JWTVerifier`
enforces `iss` and `aud` natively (verified against fastmcp 3.4.0: it rejects a
token whose `iss`/`aud` mismatch), so ADR-0002's invariant needs no extra
middleware; this is asserted by a test so an upstream regression is caught.

**Context tuple.** `agent` claim and `project` claim names are pinned here:

```python
@dataclass(frozen=True)
class RequestContext:
    principal: str            # token `sub`
    agent_session: str | None # token `agent_session` claim, optional in M0
    projects: tuple[str, ...] # token `projects` claim (may be empty)
```

- `context_from_claims(claims: Mapping[str, object]) -> RequestContext` derives the
  tuple from a verified token's claims. `sub` is required and non-empty — a verified
  token without a usable subject is an auth failure (`AuthError`, below), not a
  silent empty principal. `agent_session` is optional (M0). `projects` reads a
  `projects` list claim, defaulting to `()`.
- `current_context() -> RequestContext` is the FastMCP-facing accessor: it calls
  `fastmcp.server.dependencies.get_access_token()`, and if that returns `None`
  (no/unverified token reached the tool) raises `AuthError`; otherwise delegates to
  `context_from_claims(token.claims)`.
- `require_project(ctx, project) -> str` validates a requested `project` param is in
  `ctx.projects`, raising `AuthError` otherwise; returns the validated project. This
  is the "validate `project` against the request param" piece. **`jobs.*` take no
  `project` param, so they do not call it in M0** — it ships and is unit-tested here
  because it is auth's responsibility and the plane tools (#13+) are its first
  callers. (Stated explicitly so the function is not mistaken for dead code.)

**Failure contract.** `AuthError(message)` is a distinct exception for
"authenticated transport, but the claims are unusable for authorization" (no
subject, project not granted). It is separate from transport-level rejection: a
missing/invalid/expired bearer never reaches a tool — FastMCP's auth middleware
returns HTTP 401 before dispatch. So the acceptance's "request with no/invalid token
is rejected" is the framework's 401 (asserted via the verifier returning `None` for
a bad token), and `AuthError` covers the post-verification gaps.

### `tools/jobs.py` — handlers + register

Each tool is a thin FastMCP wrapper over a **plain async handler** that takes its
dependencies as arguments, so handlers are tested directly without MCP transport
(repo contract). The pool is the one shared dependency:

```python
async def get_job(pool, ctx, job_id: UUID) -> ToolResponse
async def wait_job(pool, ctx, job_id: UUID, timeout_s: float) -> ToolResponse
async def cancel_job(pool, ctx, job_id: UUID) -> ToolResponse
async def list_jobs(pool, ctx, *, limit: int) -> list[ToolResponse]

def register(app: FastMCP, pool: AsyncConnectionPool) -> None: ...
```

- **`get_job`** — `JOBS.get(conn, job_id)`; raises `JobNotFound` (→ tool maps to a
  `not_found`-style error response, see "Tool error mapping") if absent; else
  `ToolResponse.from_job(job)`.
- **`wait_job`** — polls `JOBS.get` every `POLL_INTERVAL` (0.5 s) until the job is
  terminal (`succeeded`/`failed`/`canceled`) or `timeout_s` elapses, then returns
  the latest envelope (terminal or the last-seen running state). `timeout_s` is
  clamped to `[0, MAX_WAIT_S]` (`MAX_WAIT_S = 300`) — an agent cannot hold a request
  open unbounded. A non-positive timeout means "one read, no wait". Uses the DB
  clock is unnecessary here; `asyncio` monotonic elapsed time bounds the loop.
- **`cancel_job`** — `JOBS.update_state(conn, job_id, CANCELED)`. The state guard
  (`state.py`) permits `queued→canceled` and `running→canceled`; a terminal job
  raises `IllegalTransition`, which the tool maps to a `configuration_error`
  response carrying the current status (cancelling a finished job is a no-op the
  agent should see, not a 500). Cooperative only: a `running` job's worker discovers
  the cancel when its fenced `complete`/`fail` misses (`state='running'` no longer
  holds) — no in-flight interrupt (that, and compensation for half-applied ops, is
  the reconciler, #12).
- **`list_jobs`** — newest-first read capped at `limit` (default 50, max 200). M0
  has one tenant and no project scoping on jobs, so the M0 filter is `ORDER BY
  created_at DESC LIMIT`. A `state`/`kind`/`project` filter arrives with RBAC (#11)
  when a caller may only see its project's jobs; shipping an unfiltered-by-project
  list now is safe **only because M0 is single-tenant** — called out so #11 tightens
  it deliberately.

`list_jobs` needs a query the repository does not expose (`get` is by-id only). A
small `recent_jobs(conn, limit) -> list[Job]` read is added to
`src/kdive/jobs/queue.py` (its stated home is "connection-scoped operations over the
durable jobs queue"), keeping all jobs SQL in one module rather than embedding SQL in
a tool.

**Tool error mapping.** A handler raising a domain error must become a
`ToolResponse` with `error_category`, not an unhandled 500. The wrappers catch:
`JobNotFound → CONFIGURATION_ERROR` (the agent referenced a non-existent id),
`IllegalTransition → CONFIGURATION_ERROR` (cancel on a terminal job),
`CategorizedError → its own .category`. The error response sets
`object_id` to the requested id, `status = "error"`, and `error_category`
accordingly. Mapping these two domain exceptions to `CONFIGURATION_ERROR` (caller
supplied a bad/again-bad id) is deliberate; an infrastructure failure
(`CategorizedError` from the pool) keeps its own category.

### `app.py` — application assembly

```python
_PLANE_REGISTRARS = (jobs.register,)  # plane issues append their register here

def build_app(pool: AsyncConnectionPool, *, verifier: JWTVerifier | None = None) -> FastMCP:
    app = FastMCP(name="kdive", auth=verifier or build_verifier())
    for register in _PLANE_REGISTRARS:
        register(app, pool)
    return app
```

`verifier` is injectable so a test builds the app with a local-keypair verifier
(no JWKS network). `build_app` does not open the pool or bind a port — the
entrypoint owns process lifecycle.

### `__main__.py` — the CLI

`python -m kdive server` and `python -m kdive worker`, `argparse` subcommands.
Both call `configure_logging(level)` first (ADR-0014; `--log-level`, default `INFO`,
also `KDIVE_LOG_LEVEL`). Shared pool construction via `db/pool.create_pool()`.

- **`server`** — open the pool, `build_app(pool)`, `app.run(transport="http",
  host=HOST, port=PORT)`. `KDIVE_HTTP_HOST` (default `127.0.0.1`) and
  `KDIVE_HTTP_PORT` (default `8000`). Default host is loopback — binding `0.0.0.0`
  is an explicit operator choice, not a default, since there is no RBAC yet (#11).
- **`worker`** — open a pool with `min_size`/`max_size ≥ 2` (the worker invariant,
  `Worker.__init__` raises otherwise), build a `HandlerRegistry` (**empty in M0** —
  plane issues register handlers; an empty registry dead-letters every job kind as
  `not_implemented`, which is the correct M0 behavior until a plane lands), construct
  `Worker(pool, registry, worker_id=…)`, install a SIGINT/SIGTERM handler that sets
  the `stop` event, and `await worker.run(stop)`. `worker_id` defaults to
  `f"{hostname}:{pid}"` so two workers never collide on the queue fence.

Per-request/per-job context binding (`bind_context`, ADR-0014): the worker already
binds `job_id`; tool calls bind `request_id` + `principal` around the handler so logs
carry the attribution. `request_id` is the MCP request id when available, else a
process-monotonic counter (no `uuid4`/`Random` — deterministic, and ADR-0014's
context is for correlation, not security).

## Failure modes & edges (drives the tests)

- **No token / invalid / expired / wrong `iss` / wrong `aud`** → verifier returns
  `None` → FastMCP 401; tool never runs. Tested at the verifier level with
  `RSAKeyPair`-minted tokens (valid, wrong-iss, wrong-aud, expired) asserting
  `verify_token` returns a token only for the fully-valid one.
- **Valid token, missing `sub`** → `context_from_claims` raises `AuthError` (not an
  empty principal).
- **Valid token, `agent_session` absent** → context has `agent_session=None`
  (allowed in M0).
- **`require_project` with a project not in claims** → `AuthError`.
- **`jobs.get` unknown id** → error envelope, `error_category=configuration_error`.
- **`jobs.get` known id in each state** → envelope with the right `status`,
  `suggested_next_actions`, and `refs` (`result` present only when `result_ref`
  set).
- **`jobs.cancel` on queued/running** → `canceled` envelope. **on terminal** →
  error envelope (`configuration_error`), status reflects the unchanged terminal
  state.
- **`jobs.wait` already-terminal** → returns immediately (no full timeout wait).
  **`jobs.wait` times out on a still-running job** → returns the running envelope
  after ≤ `timeout_s`. **negative/oversized timeout** → clamped.
- **`jobs.list` empty / fewer-than-limit / more-than-limit** → correct count, order
  newest-first, never exceeds `limit`.
- **`from_job` invariant** → a `failed` job missing `error_category`, or a
  non-`failed` job carrying one, raises `ValueError`.
- **`build_verifier` missing any env var** → `CategorizedError(CONFIGURATION_ERROR)`
  naming the var.

## Testing strategy

Handlers and pure functions are the unit of testing (repo contract): call
`get_job`/`wait_job`/`cancel_job`/`list_jobs` directly with an injected pool
(testcontainers Postgres, the `migrated_url` fixture) and a hand-built
`RequestContext`; never drive them through the MCP transport. Auth is tested at the
function level with `RSAKeyPair`-minted tokens and an in-process `JWTVerifier`
(public-key mode, no JWKS network). `from_job`/`suggested_next_actions` are pure and
tested without a DB. The CLI's argument parsing is tested by invoking the parser;
the run loops (`app.run`, `worker.run`) are not started in unit tests. Async tests
follow the repo idiom — a sync test wrapping `asyncio.run(_run())`. No new gated
integration tests; nothing here needs libvirt/gdb/drgn.
