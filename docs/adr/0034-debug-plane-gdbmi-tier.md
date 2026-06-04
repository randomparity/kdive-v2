# ADR 0034 — Debug plane: gdb-MI tier (M0)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** #21 (M0: Debug plane: port gdb-MI tier)
- **Depends on:** [ADR-0032](0032-connect-plane-gdbstub-debugsession.md) (the
  `DebugSession` `attach → live → detached` row, the `debug.start_session`/`end_session`
  tools, and the `TransportHandleData` `gdbstub://host:port` handle this plane keys its
  live gdb/MI engine on; ADR-0032 §"Considered & rejected" explicitly defers the persistent
  gdb/MI subprocess to this issue),
  [ADR-0027](0027-safety-modules-secret-backend-impl.md) (the `Redactor` every
  transcript/record passes through),
  [ADR-0019](0019-tool-response-envelope.md) (the response envelope),
  [ADR-0020](0020-rbac-audit-gate-implementation.md) (the `operator` role + audit record),
  [ADR-0009](0009-capability-provider-dispatch.md) (the `DebugPlane` capability this
  realizes).
- **Spec:** [`../superpowers/specs/2026-06-04-debug-plane-gdbmi-design.md`](../superpowers/specs/2026-06-04-debug-plane-gdbmi-design.md)

## Context

A `live` `DebugSession` (#20) records an open single-attach gdbstub transport to a booted,
`ready` System. #20 returns no guest bytes. Issue #21 adds the **Debug plane**: constrained
debug operations over that stub via a gdb-MI tier ported from v1
`providers/local/debug/gdb_mi.py`, exposing seven synchronous tools (`debug.set_breakpoint`,
`.clear_breakpoint`, `.list_breakpoints`, `.read_memory`, `.read_registers`, `.continue`,
`.interrupt`). The decisions the v1 port leaves open for the v2 layering are settled here.

## Decision

### 1. The gdb/MI engine is lazily attached on first op, cached in a process-scoped registry keyed on `session_id`

#20 returns only an RSP-reachability proof + endpoint; it opens no gdb subprocess. The
gdb/MI engine is opened lazily on the first `debug.*` op for a session and cached in an
in-process `GdbMiSessionRegistry` (`session_id → GdbMiAttachment`). This keeps #20 unchanged
(no schema, no new column), and matches v1 ADR-0021: the live engine is
server-process-scoped and non-durable. A server restart strands the attachment; the next op
gets `no_live_session` (`CONFIGURATION_ERROR`), and the agent must end + restart the session.

### 2. Single `live_vm`-gated seam: the lazy attach (gdb spawn + RSP connect + symbol path)

Everything above the attach — handler authz/state gate, registry lookup, and every engine op
driven against an injected fake `MiController` — is unit-tested off the gate. The only
`live_vm`-gated seam is the attach itself (spawn `gdb --interpreter=mi3`, connect RSP, load
the vmlinux symbol path resolved from the Run/System). Its default raises `MISSING_DEPENDENCY`
outside the gate, surfaced as `DEBUG_ATTACH_FAILURE`, so the lazy-attach error contract is
unit-tested with a fake attach seam. This mirrors the Connect plane's single-seam shape.

### 3. `read_memory` returns guest bytes **verbatim** under the 4096-byte cap — never redacted

The 4096-byte cap (ported v1 `MAX_MEMORY_READ_BYTES`) is the memory-read safety control,
enforced at the handler boundary: `byte_count > 4096` is a `CONFIGURATION_ERROR` raised
**before any MI command runs**. The bytes that come back are returned **verbatim** — the
`Redactor` is a text/structure masker, and running raw binary guest memory through it would
silently corrupt the dump the agent explicitly requested. The bytes are surfaced as a
lossless lowercase-hex string in `data["memory_hex"]` (the envelope's `data` is
`dict[str, str]`; hex is a faithful rendering of opaque bytes, not a redaction).

### 4. All **textual** transcript/record output passes through the `Redactor` before persist and response

The transcript JSON-lines file (per session, under the run dir) and every record-derived
response field — `BreakpointRef`, `StopRecord`, `read_registers` — are run through the merged
#25 `Redactor` before they are written or returned. The redactor seeds from
`PROCESS_SECRET_REGISTRY`, so a registered secret appearing in any transcript/record text is
masked without the call site re-supplying it. This is the boundary the live acceptance
exercises (a known secret in the gdb-MI textual transcript is masked in the response), and it
is **orthogonal** to decision 3: text is masked, requested memory bytes are not.

### 5. The seven tools are synchronous — no `JobKind`, no handler registrar

A breakpoint/read/resume is a bounded MI round-trip, not a minutes-long provider op. Per
ADR-0032 §3 ("everything else … is synchronous"), the seven tools are plain async handlers
that do their work inline and return a terminal envelope — no `JobKind`, no
`_HANDLER_REGISTRARS` entry, no `jobs_kind_check` change. They are added to the **existing**
`debug.py` `register(app, pool)` (#20's), so `app.py` needs **no** new `_PLANE_REGISTRARS`
append either.

### 6. The v1 `GdbMiError` folds into the existing `CategorizedError` — no new exception type

v1 carried a `GdbMiError(CategorizedError)` subclass. v2 already has `CategorizedError` with
the same `category`/`details` shape, and handlers map on `category` alone. Per the global
"replace, don't deprecate" rule, the engine raises `CategorizedError` directly; no parallel
exception type is introduced.

### 7. Only the seven-tool engine surface ports; the rest of the 854-LOC v1 engine is dropped

The in-scope surface is the seven tools' engine ops plus their typed records, the
`MiController` seam, the execution-control (resume/wait/interrupt) machinery, the redacted
transcript, and the session registry. The v1 attach/`probe_read`/`resolve_symbol`/
`load_module_symbols`/`read_symbol`/`evaluate_inspector`/`backtrace`/`list_variables`/
`set_watchpoint`/`step`/`next`/`finish` surface is **not** ported here — those belong to the
Connect-plane attach (#20, done differently) and to later introspection/postmortem
milestones. Carrying them now would be dead code that misleads readers and the type checker.

## Consequences

- The Debug plane is fully unit-testable with a fake `MiController` + fake attach seam; the
  real gdb/RSP path is `live_vm`-gated, so CI stays green with no gdb/KVM host.
- The live gdb/MI engine is in-process and non-durable, keyed on the existing
  `transport_handle`/`session_id`; no schema migration, no `JobKind`, no new column.
- `read_memory` bytes are verbatim under a 4096 cap; transcript/record **text** is redacted.
  The two controls are independent and both covered by the acceptance test.
- `debug.py` gains seven handlers + registrations on its existing `register`; `app.py` is
  unchanged. `docs/adr/README.md` and `tests/mcp/test_app.py` get minimal additive edits also
  touched by #22.
- `pygdbmi==0.11.0.0` is added as a runtime dependency (the gdb/MI parser + subprocess
  driver). Its parser is used in tests too (`parse_mi_records`), so it is a hard dep, not a
  `live_vm` extra.

## Considered & rejected

- **Port the whole 854-LOC v1 engine verbatim.** Rejected: issue #21 scopes seven tools; the
  attach/symbol-resolution/module-loading/stack/watchpoint/evaluate surface is out of scope
  and belongs to #20 (done) or later milestones. Carrying it as unused code violates the
  no-phantom-features / no-dead-code rules and would force the type checker and readers to
  reason about surface no tool reaches. A later issue ports what it needs.
- **Open the gdb/MI subprocess eagerly in #20's `start_session`.** Rejected: that would
  change #20 (already merged) and pin a gdb process + RSP socket for the life of every
  `live` session even if no Debug-plane op ever runs. ADR-0032 explicitly defers the
  persistent subprocess to this issue. Lazy attach on first op, cached in the registry,
  opens the engine only when needed.
- **Persist the live attachment durably (a DB column / table) so it survives restart.**
  Rejected: an open gdb subprocess + RSP socket is process-local OS state; it cannot be
  rehydrated from a row. v1's ADR-0021 contract is honored: the registry is in-process, a
  restart strands it, and the next op returns `no_live_session` so the agent re-establishes.
- **Run `read_memory` bytes through the `Redactor`.** Rejected: the redactor masks text and
  structured key/value text; binary guest memory is opaque bytes, and masking would corrupt
  the requested dump. The cap is the memory control; redaction is the transcript-text
  control. The bytes are returned verbatim as lossless hex.
- **Drop the 4096 cap or make it configurable.** Rejected: it is a ported invariant and a
  safety bound on how much guest memory one synchronous op returns; #21 names it explicitly.
  No caller needs a larger read in M0, so a config knob would be a speculative feature.
- **Introduce a new `GdbMiError` exception subclass.** Rejected: `CategorizedError` already
  carries `category`/`details`, and handlers map on `category`. A parallel subclass is the
  kind of dual-path shim the "replace, don't deprecate" rule forbids.
- **Make the Debug ops jobs (`JobKind.DEBUG_*`) with worker handlers.** Rejected: each op is
  a bounded MI round-trip, classified synchronous by ADR-0032 §3. A job would add a schema
  constraint value, a handler registrar, and an admission/poll round-trip for no latency
  benefit.
- **Add a new `_PLANE_REGISTRARS` entry for the Debug plane.** Rejected: #20 already
  registers the `debug.*` plane via `debug.register`; the seven tools are added to that same
  `register`, so `app.py` is untouched — minimizing edits to a file a sibling issue (#22)
  may also touch.
