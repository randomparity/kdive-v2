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

  **M0 isolation posture (explicit, not "single-tenant").** ADR-0002 is *multi-user*
  OIDC, so several human principals can authenticate against one M0 deployment.
  Until RBAC (#11), `jobs.get`/`jobs.list`/`jobs.cancel` are **not** scoped to the
  caller: any authenticated principal can read and **list every principal's jobs**,
  and `jobs.cancel` is a **state mutation** any authenticated principal can apply to
  any job. This is a deliberate, bounded exposure, not an oversight:

  - M0 *cannot* scope on the job's `authorizing` tuple yet — its interior shape is
    "owned by a later issue" (`domain/models.py`: `authorizing: dict[str, Any]`), so
    no stable key exists to filter on. Scoping arrives with #11, which pins the
    tuple and adds the role/ownership checks together.
  - **Risk window:** the cross-principal read/cancel exposure is accepted only for
    M0's trusted-operator deployment (a small known set of principals). #11 **must**
    close it before any broader/untrusted multi-principal use. Recorded here so #11
    treats it as a gating prerequisite, not a nice-to-have.
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

  **Verified (fastmcp 3.4.0).** `JWTVerifier.verify_token` constructs
  `AccessToken(claims=<full decoded claim set>)`, so *custom* claims
  (`agent_session`, `projects`) survive verification — confirmed empirically by
  minting a token with both and asserting they appear in `token.claims`. **Gotcha:**
  the same check showed `AccessToken.subject` is `None` even for a token with a valid
  `sub` claim (FastMCP does not populate it from `sub`). Therefore
  `context_from_claims` reads the principal from `claims["sub"]`, **not**
  `token.subject`; a test asserts this so an upstream change that starts populating
  `subject` (or stops passing custom claims) is caught.

  **Provisional — the `projects` claim name/shape is not yet pinned by ADR-0002.**
  ADR-0006 defers token/claim shape to ADR-0010, and the IdP membership claim's name
  (`projects` vs. `groups` vs. a `project→role` map) is not established in any cited
  ADR. `projects: list[str]` is this spec's provisional choice; **#11/#13 must
  confirm it against the real IdP/ADR-0002 before consuming it**, and may rename it.
  It is pinned here only so `require_project` has a shape to compile against; no M0
  tool depends on its value.
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
  open unbounded. A non-positive timeout means "one read, no wait". The loop deadline
  is an `asyncio` monotonic elapsed-time bound (no DB clock needed).

  **Connection discipline:** each poll acquires a pool connection for the single
  `JOBS.get` read and **releases it before sleeping** (`async with
  pool.connection()` scoped to the read), so a waiting request holds **zero**
  connections between polls — N concurrent waiters cost N connections only at the
  poll instant, not for the whole wait. **`MAX_WAIT_S` must be ≤ the transport's
  request timeout** (the ASGI server's, and any reverse proxy's, idle/request
  timeout); otherwise a proxy closes the connection and the agent sees a transport
  error instead of an envelope. The `server` entrypoint pins the ASGI request
  timeout (or documents the proxy requirement) so this invariant holds — see
  `__main__.py §server`.
- **`cancel_job`** — `JOBS.update_state(conn, job_id, CANCELED)`. The state guard
  (`state.py`) permits `queued→canceled` and `running→canceled`; a terminal job
  raises `IllegalTransition`, which the tool maps to a `configuration_error`
  response carrying the current status (cancelling a finished job is a no-op the
  agent should see, not a 500). Cooperative only: a `running` job's worker discovers
  the cancel when its fenced `complete`/`fail` misses (`state='running'` no longer
  holds) — no in-flight interrupt (that, and compensation for half-applied ops, is
  the reconciler, #12).
- **`list_jobs`** — newest-first read capped at `limit` (default 50, max 200): the
  M0 query is `ORDER BY created_at DESC LIMIT`. A `state`/`kind`/`project` filter and
  per-principal scoping arrive with RBAC (#11) — see the M0 isolation posture in
  Non-goals for why the list is unscoped now.

  **Per-row failure isolation.** `list_jobs` maps `ToolResponse.from_job` over every
  returned row, and `from_job` raises on a row that violates the "category iff
  failed" invariant (ADR-0019). A single malformed row (e.g. a future plane writes a
  `failed` job with no `error_category`) must **not** blank the entire list. So
  `list_jobs` builds each envelope in isolation: a row whose `from_job` raises is
  logged (with its job id) and replaced by a degraded error envelope
  (`status="error"`, `error_category=infrastructure_failure`, `object_id` = the bad
  row's id) rather than propagating. The agent still sees every other job. The strict
  validator stays fail-closed for single-object `jobs.get`, where one bad row *is*
  the whole answer.

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

### `app.py` — application assembly + the two plane seams

A plane issue (#11+) ships **two** things: a tool surface *and* a job handler. The
skeleton therefore exposes **two symmetric registrar seams**, so a plane is added by
appending to a tuple in one place and never edits the entrypoint:

```python
# Tool seam: each plane module exposes register(app, pool); app.py calls them all.
_PLANE_REGISTRARS = (jobs.register,)

# Handler seam: each plane module exposes register_handlers(registry); the worker
# calls them all. Empty of real handlers in M0 (jobs.* register no JobHandler — they
# are read/cancel tools, not job kinds), but the seam exists now so a plane plugs in
# without touching __main__.py.
_HANDLER_REGISTRARS: tuple[Callable[[HandlerRegistry], None], ...] = ()

def build_app(pool: AsyncConnectionPool, *, verifier: JWTVerifier | None = None) -> FastMCP:
    app = FastMCP(name="kdive", auth=verifier or build_verifier())
    for register in _PLANE_REGISTRARS:
        register(app, pool)
    return app

def build_handler_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    for register in _HANDLER_REGISTRARS:
        register(registry)
    return registry
```

Both seams live in `app.py` so the list of planes is in one file. `verifier` is
injectable so a test builds the app with a local-keypair verifier (no JWKS network).
`build_app` does not open the pool or bind a port — the entrypoint owns process
lifecycle. The asymmetry the review flagged — a tool hook with no handler twin — is
closed: `__main__.py worker` calls `build_handler_registry()` instead of hand-wiring
an empty `HandlerRegistry`, so the M0 worker and every future plane share one seam.

### `__main__.py` — the CLI

`python -m kdive server` and `python -m kdive worker`, `argparse` subcommands.
Both call `configure_logging(level)` first (ADR-0014; `--log-level`, default `INFO`,
also `KDIVE_LOG_LEVEL`). Shared pool construction via `db/pool.create_pool()`.

- **`server`** — open the pool, `build_app(pool)`, `app.run(transport="http",
  host=HOST, port=PORT)`. `KDIVE_HTTP_HOST` (default `127.0.0.1`) and
  `KDIVE_HTTP_PORT` (default `8000`). Default host is loopback — binding `0.0.0.0`
  is an explicit operator choice, not a default, since there is no RBAC yet (#11).

  **Server pool sizing + long-poll timeout.** The server pool is opened with
  `min_size=1, max_size=KDIVE_HTTP_POOL_MAX` (default `10`). Because `wait_job`
  releases its connection between polls (see `wait_job`), concurrent waiters do not
  pin the pool; `max_size=10` bounds simultaneous *in-flight* tool reads, and the
  default is overridable for load. The ASGI request/keep-alive timeout is configured
  **≥ `MAX_WAIT_S`** (300 s) so a long `jobs.wait` is not severed mid-poll; if an
  operator fronts the server with a reverse proxy, the proxy's read timeout must also
  be ≥ `MAX_WAIT_S` — documented as a deployment requirement, since the proxy is
  outside this process's control.
- **`worker`** — open a pool with `min_size`/`max_size ≥ 2` (the worker invariant,
  `Worker.__init__` raises otherwise), build the registry via
  `app.build_handler_registry()` (the handler seam — **empty of real handlers in
  M0**, so the worker dead-letters every job kind as `not_implemented`, the correct
  M0 behavior until a plane registers a handler), construct
  `Worker(pool, registry, worker_id=…)`, install a SIGINT/SIGTERM handler that sets
  the `stop` event, and `await worker.run(stop)`. `worker_id` defaults to
  `f"{hostname}:{pid}"` so two workers never collide on the queue fence.

Per-request/per-job context binding (`bind_context`, ADR-0014): the worker already
binds `job_id`; tool calls bind `request_id` + `principal` around the handler so logs
carry the attribution. `request_id` is the MCP request id when available, else a
`uuid4` hex (a bare per-process counter would collide across server processes and
restarts, defeating cross-fleet log correlation — the very purpose of the id).

**Auth-rejection observability.** A no/invalid/expired-token request is rejected by
FastMCP's auth middleware *before* a tool runs, so kdive's `bind_context` JSON logs
never fire for it. To keep an auth-failure spike (misconfig or attack) from being a
blind spot, the `server` entrypoint leaves FastMCP/Starlette's access logging enabled
(rejections appear there as 401s) and sets the FastMCP auth logger to propagate to the
root handler, so its "Bearer token rejected …" warnings land in the same stderr JSON
stream. No bespoke auth-metric is built in M0; the requirement is only that rejections
are *visible*, not silent.

## Failure modes & edges (drives the tests)

- **No token / invalid / expired / wrong `iss` / wrong `aud`** → verifier returns
  `None` → FastMCP 401; tool never runs. Tested at the verifier level with
  `RSAKeyPair`-minted tokens (valid, wrong-iss, wrong-aud, expired) asserting
  `verify_token` returns a token only for the fully-valid one.
- **Valid token, custom claims present** → `context_from_claims` reads `principal`
  from `claims["sub"]` (not `token.subject`, which FastMCP leaves `None`), and
  `agent_session`/`projects` from their claims; a test mints a token carrying all
  three and asserts they survive verification and land in the context.
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
- **`jobs.list` with one invariant-violating row** → that row becomes a degraded
  error envelope; every other job still appears (per-row isolation), and the list
  call does not raise.
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
