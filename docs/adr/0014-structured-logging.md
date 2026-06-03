# ADR 0014 — Structured logging via stdlib `logging` + `contextvars`

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-03
- **Deciders:** D. Christensen (core platform)

## Context

The platform is an async core (FastMCP streamable-HTTP over async `psycopg`) plus a
worker tier and a reconciler, running as separate processes. Operators need to
correlate a single request or job as it moves across those processes, so every log
line must carry a context tuple — request id, job id, principal, object id, and the
state transition in flight.

The M0 plan's first issue (`../plans/m0-implementation.md`, "Issue 1 — Repo
scaffolding & tooling") requires a `kdive/log.py` that configures stdlib `logging`
for JSON/key-value output with that context **and adds no new dependency**. The
top-level design states that tool responses carry artifact *references*, "never log
dumps" (`../specs/top-level-design.md`), and a separate safety layer (Issue 23 —
`security/redaction.py`) must be able to scrub registered secrets from log records
without every emit site cooperating.

The binding forces:

- **Async-correct context.** Context must survive an `await` and stay isolated
  between concurrently in-flight requests multiplexed on one event-loop thread.
- **No per-call boilerplate.** Emit sites should call `logger.info("msg")`; they
  must not have to assemble and thread a context dict through every call.
- **A redaction attachment point.** Issue 23 must add secret-scrubbing as an
  additional filter, not a rewrite of the logging path.
- **Zero new dependency** for this foundation.

## Decision

We will build structured logging on the Python standard library only:

- A `logging.Formatter` subclass that serializes each record to one JSON line over a
  fixed field schema (timestamp, level, logger, message, plus the context fields).
- A `logging.Filter` that copies a fixed set of `contextvars.ContextVar`s
  (`request_id`, `job_id`, `principal`, `object_id`, `transition`) onto each
  `LogRecord` as it is emitted.
- A `bind_context(**fields)` context manager that sets those vars and resets them on
  exit via the returned tokens, so context cannot leak past its scope.
- `configure_logging(level)` that installs the formatter + filter on the root logger
  **idempotently** (safe to call from each entrypoint).

Server, worker, and reconciler call `configure_logging()` at startup; later issues
emit through the standard `logging` API and bind context with `bind_context(...)`.

## Consequences

What becomes easier:

- Any `logger.info("msg")` automatically carries the active request/job context with
  no boilerplate, and the context is async-correct (per-task, survives awaits).
- Issue 23's redactor attaches as one additional `logging.Filter` on the root
  handler without touching call sites.
- No third-party logging dependency to audit, pin, or update.

What becomes harder / new obligations:

- Context vars must be bound at the right scope — per request in the MCP middleware
  (#8/#10), per job at claim time (#7/#8), per loop pass in the reconciler (#10) —
  and reset on exit. The `bind_context` context manager makes the leak path the
  unusual one rather than the default.
- The JSON field schema is owned in code; adding a field is a code change, not
  configuration. This is acceptable for a fixed, audited log shape.
- Cross-process correlation relies on callers propagating the request id; M0 ships no
  distributed tracing.

## Alternatives considered

- **`structlog` / `python-json-logger`.** Rejected: the plan forbids a new dependency
  for this foundation, and both add supply-chain surface for what a small amount of
  stdlib code achieves. `structlog`'s processor pipeline is more machinery than the
  M0 log shape needs.
- **`threading.local` for context.** Rejected: the core is `asyncio`. Thread-locals
  are shared across coroutines multiplexed on a single event-loop thread, so
  concurrent requests would overwrite each other's context. `contextvars` is the
  async-aware replacement designed for exactly this.
- **`logging.LoggerAdapter` / explicit `extra=` at each call site.** Rejected: this
  forces every emit site to assemble and pass the context dict — the precise
  boilerplate this seam exists to remove — and a forgotten `extra=` silently drops
  context with no error.
