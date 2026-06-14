# Plan — `kdivectl tool call` mutating/destructive opt-in (#368)

- **Spec:** [`../specs/2026-06-13-cli-mutating-tool-call.md`](../specs/2026-06-13-cli-mutating-tool-call.md)
- **ADR:** [ADR-0105](../../adr/0105-cli-mutating-tool-call-opt-in.md)
- **Issue:** #368
- **Branch:** `feat/kdivectl-mutating-tools-368`

This change is implemented directly in one session (the tasks are tightly coupled: the
classifier, the dispatch flow, and the parser flags all move together). Each task below is TDD:
write the failing test, confirm it fails for the right reason, write the minimal code, run the
focused test, then the guardrails. Guardrails: `just lint`, `just type`, `just test` before each
commit; `just ci` before push. Conventional Commit subjects ≤72 chars with the
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

## Conventions (apply to every task)

- Python 3.13, `uv`. Absolute imports only. ≤100 lines/function, complexity ≤8, 100-char lines,
  Google-style docstrings on non-trivial public APIs. No commented-out code.
- The whole `kdive.cli.*` package imports no `kdive.services.*` and reads no DB/object-store
  credential (ADR-0089 decision 5; `test_no_service_import` enforces it on the import graph). The
  classifier and dispatch changes touch only `kdive.cli.*` and `mcp.types`/`fastmcp`, so the
  boundary holds — do not import anything under `kdive.services` or `kdive.db`.
- Pick the most specific structured error; redact nothing new (no secrets flow through these paths;
  the token is never printed).

## Task 1 — Tier classifier in `kdive.cli.passthrough`

**Where it fits:** the binary `assert_read_only` becomes a tiered classifier — the foundation the
dispatch flow and admission build on.

**Files:** `src/kdive/cli/passthrough.py` (rewrite), `tests/cli/test_passthrough.py` (rewrite).

**Steps (TDD):**

1. Write failing tests in `tests/cli/test_passthrough.py`:
   - `classify_tool` returns `ToolTier.READ_ONLY` for a tool with `readOnlyHint=True`
     (regardless of `destructiveHint`), `DESTRUCTIVE` for `readOnlyHint=False, destructiveHint=True`,
     `MUTATING` for `readOnlyHint=False` with `destructiveHint` `False`/`None`/absent, and `UNKNOWN`
     for: a missing-annotations object, `None`, a `readOnlyHint=None`, and a truthy-non-`True`
     (`1`) `readOnlyHint`.
   - Precedence: a tool with `readOnlyHint=True` **and** `destructiveHint=True` classifies
     `READ_ONLY` (READ_ONLY dominates).
2. Confirm they fail (the symbol does not exist yet).
3. Implement:
   - `class ToolTier(enum.Enum)` with members `READ_ONLY`, `MUTATING`, `DESTRUCTIVE`, `UNKNOWN`,
     and a private monotonic rank for the three admissible tiers so admission can compare. Put the
     rank in a module-level mapping `_RANK = {READ_ONLY: 0, MUTATING: 1, DESTRUCTIVE: 2}`
     (UNKNOWN deliberately not ranked — it is never admitted).
   - `classify_tool(tool: object) -> ToolTier` reading `annotations = getattr(tool, "annotations",
     None)`, `ro = getattr(annotations, "readOnlyHint", None)`, `dh = getattr(annotations,
     "destructiveHint", None)`. Order: `ro is True` → READ_ONLY; `ro is False` →
     (`dh is True` → DESTRUCTIVE else MUTATING); else UNKNOWN.

**Acceptance:** all classifier tests pass; `classify_tool(None) is ToolTier.UNKNOWN`;
`READ_ONLY` wins over a co-set destructive hint.

## Task 2 — Admission `assert_tool_allowed`

**Where it fits:** the gate `_tool_call` calls; turns a tier + `max_tier` into admit/refuse and
returns the resolved tier.

**Files:** `src/kdive/cli/passthrough.py`, `tests/cli/test_passthrough.py`.

**Steps (TDD):**

1. Failing tests:
   - `assert_tool_allowed` returns the tier for an admitted call (READ_ONLY at any `max_tier`;
     MUTATING at `max_tier=MUTATING` and `DESTRUCTIVE`; DESTRUCTIVE only at `max_tier=DESTRUCTIVE`).
   - Raises `ToolNotAllowedError` for: MUTATING at `max_tier=READ_ONLY`; DESTRUCTIVE at
     `max_tier=MUTATING`; and `UNKNOWN` at **every** `max_tier` including `DESTRUCTIVE`.
   - The refusal message for a MUTATING refusal names `--allow-mutating`; for DESTRUCTIVE names
     `--allow-destructive`; for UNKNOWN names neither flag (states "not positively classified").
   - The message names the tool.
2. Confirm failure.
3. Implement `class ToolNotAllowedError(RuntimeError)` and
   `assert_tool_allowed(name, tool, *, max_tier) -> ToolTier`: classify; if `UNKNOWN` raise
   (no-flag message); else if `_RANK[tier] <= _RANK[max_tier]` return tier; else raise with the
   tier-specific flag in the message. Remove `NotReadOnlyError` and `assert_read_only` entirely
   (no shim).

**Acceptance:** admission tests pass; `UNKNOWN` refused at `DESTRUCTIVE`; messages name the right
flag and the tool. `rg "assert_read_only|NotReadOnlyError" src tests` returns no hits.

## Task 3 — Destructive confirmation helper

**Where it fits:** the TTY/`--yes`/EOF decision, isolated so it is unit-tested without a terminal.

**Files:** `src/kdive/cli/dispatch.py`, `tests/cli/test_tool_call.py` (new).

**Steps (TDD):**

1. Failing tests for `_confirm_destructive(name, *, assume_yes, is_tty, read_line) -> bool`:
   - `assume_yes=True` → `True` (no read attempted; pass a `read_line` that raises if called).
   - `assume_yes=False, is_tty=True`, `read_line` returns `"yes"` → `True`; returns `"no"` /
     `""` / raises `EOFError` → `False`.
   - `assume_yes=False, is_tty=False` → `False` **without** calling `read_line` (non-TTY, no
     `--yes` is an immediate refuse).
2. Confirm failure.
3. Implement: if `assume_yes` return True; if not `is_tty` return False; else call `read_line`
   (catching `EOFError` → False) and return `answer.strip() == "yes"`. `read_line` defaults to a
   thin wrapper that prints the prompt and reads `input()`; injected in tests.

**Acceptance:** helper tests pass; non-TTY-no-yes never reads; EOF → False.

## Task 4 — Tiered `_tool_call` dispatch (preflight + confirm + envelope exit)

**Where it fits:** the actual passthrough flow that wires Tasks 1–3 plus the exit-code derivation.

**Files:** `src/kdive/cli/dispatch.py`, `tests/cli/test_tool_call.py`.

**Steps (TDD):**

1. Failing tests (drive `_tool_call` with a fake session/client, monkeypatching the module seams —
   mirror `tests/cli/test_mutation_verbs.py`'s fake pattern). Build an `args` namespace with
   `name`, `payload`, `allow_mutating`, `allow_destructive`, `yes`:
   - Mutating tool, no tier flag → exit 3, `client.call_tool` not called.
   - Mutating tool, `allow_mutating=True` → `client.call_tool` called once; no confirmation
     read; exit from the (success) envelope is 0.
   - Mutating/destructive tool, expired token (real `ensure_token_valid` with a token whose
     `exp` is in the past, or monkeypatch it to raise `TokenExpiringError`) → exit 3,
     `client.call_tool` not called. Read-only tool with the same expired token → still dispatched
     (no preflight).
   - Destructive tool, `allow_destructive=True`, non-TTY, `yes=False` → exit 3, not called,
     stdout/err message contains `--yes`.
   - Destructive tool, `allow_destructive=True`, `yes=True` → called once.
   - Admitted mutating call whose fake envelope carries
     `error_category="authorization_denied"` → exit 3 (derived), even though the call was
     dispatched.
   - Admitted call with a clean envelope → exit 0.
   - Read-only tool, no flags → dispatched, exit 0 (unchanged default).
2. Confirm failure.
3. Implement `_tool_call`:
   - Resolve `max_tier` from `args.allow_destructive` / `args.allow_mutating`.
   - List tools; `assert_tool_allowed(args.name, tools.get(args.name), max_tier=max_tier)` inside a
     try; on `ToolNotAllowedError` print + return `_TIER_NOT_ALLOWED_EXIT` (rename
     `_NOT_READ_ONLY_EXIT`).
   - If tier in (MUTATING, DESTRUCTIVE): `ensure_token_valid(session.token, now=int(time.time()))`
     inside try; on `TokenExpiringError` print + return 3.
   - If tier is DESTRUCTIVE: `if not _confirm_destructive(args.name, assume_yes=args.yes,
     is_tty=sys.stdin.isatty(), read_line=...)`: print the `--yes` message + return 3.
   - `result = await client.call_tool(...)`; `print(json.dumps(tool_envelope(result), indent=2,
     default=str))`; `return exit_code_for_envelope(tool_envelope(result))`.
   - Import `ensure_token_valid`, `TokenExpiringError` from `kdive.cli.commands.mutations`,
     `exit_code_for_envelope` from `kdive.cli.errors`, `tool_envelope` already imported.

**Acceptance:** all dispatch tests pass; the eight success criteria's dispatch rows hold.

## Task 5 — Parser flags

**Where it fits:** surfaces the three flags on `tool call`; without them the namespace lacks
`allow_mutating`/`allow_destructive`/`yes` and Task 4's resolution breaks.

**Files:** `src/kdive/cli/__main__.py`, `tests/cli/test_tool_call.py` (or `test_dispatch_wiring.py`).

**Steps (TDD):**

1. Failing tests on `build_parser().parse_args([...])`:
   - `tool call x` → `allow_mutating is False`, `allow_destructive is False`, `yes is False`.
   - `--allow-mutating`, `--allow-destructive`, `--yes` each set their dest True.
   - `--json '{...}'` still parses as `payload` (no collision with the new flags).
2. Confirm failure.
3. Add to the `call` subparser: `--allow-mutating` (dest `allow_mutating`, store_true),
   `--allow-destructive` (dest `allow_destructive`, store_true), `--yes` (dest `yes`, store_true).
   Update the subparser help text ("generic MCP passthrough" → note the opt-in tiers).

**Acceptance:** flag-parsing tests pass; existing `--json` payload test still passes.

## Task 6 — Guardrails, docs cross-check, full CI

1. `rg "assert_read_only|NotReadOnlyError|_NOT_READ_ONLY_EXIT"` over `src` + `tests` → no stale
   references (dead-code check per CLAUDE.md "replace, don't deprecate").
2. `just lint`, `just type`, `just test` green. Then `just ci`.
3. Confirm `tests/mcp/test_read_tools_annotated.py` still passes unchanged (the server-side
   annotation guard is untouched).

**Acceptance:** `just ci` green; no stale symbol references; annotation-guard tests pass.

## Rollback / cleanup

Pure client-side, additive flags + an internal gate rewrite; no migration, no schema, no server
change. Rollback is reverting the branch. No external state is created. The only removed public
symbols (`assert_read_only`, `NotReadOnlyError`) are CLI-internal (no external importer; verified by
the rg sweep in Task 6).

## Verification gaps / notes

- These are unit/CLI tests driven at the `_tool_call` / `assert_tool_allowed` boundary with injected
  fakes (no live MCP server) — the project convention for the CLI (`tests/cli/*` already test this
  way). A live end-to-end exercise over the real transport is the coverage-campaign rerun
  (`docs/runbooks/mcp-coverage-campaign-rerun.md`), out of scope here.
- `sys.stdin.isatty()` is the only real-environment read; it is injected into `_confirm_destructive`
  as `is_tty` so tests are deterministic. `_tool_call` itself computes `is_tty` once and passes it.
