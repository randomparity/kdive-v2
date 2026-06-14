# Spec — `kdivectl tool call` mutating/destructive opt-in (#368)

- **Date:** 2026-06-13
- **Issue:** [#368](https://github.com/randomparity/kdive/issues/368) (coverage campaign F1)
- **ADR:** [ADR-0107](../../adr/0107-cli-mutating-tool-call-opt-in.md)
- **Status:** Draft

## Problem

`kdivectl tool call <name>` is read-only by construction: `kdive.cli.passthrough.assert_read_only`
admits a tool only when its MCP `readOnlyHint` is exactly `True` and raises `NotReadOnlyError`
(exit 3) otherwise. Mutating (`mutating()`) and destructive (`destructive()`) tools are therefore
unreachable via the passthrough, leaving the bulk of the 91-tool surface drivable only through a
small set of curated verbs. An operator or agent restricted to the shipped CLI cannot administer or
drive most of the platform.

The CLI is a pure MCP client; the server-side destructive-op gate (capability + RBAC + profile
opt-in, deny by default) is the authorization boundary and applies to every call. The read-only gate
is a client-side UX guard, not a security control. This change relaxes that guard from "refuse all
mutation" to "refuse mutation unless the caller explicitly opts in," per ADR-0107.

## Goals

1. `kdivectl tool call <name>` can reach a `mutating()`-annotated tool when the caller passes
   `--allow-mutating`.
2. `kdivectl tool call <name>` can reach a `destructive()`-annotated tool when the caller passes
   `--allow-destructive` **and** confirms (typed `yes` on a TTY, or `--yes` for non-interactive use).
3. With no opt-in flag, behavior is **identical to today**: only read-only tools admitted; mutating,
   destructive, unannotated, and unknown tools refused with exit 3.
4. An unannotated / unresolvable / not-positively-classified tool (`UNKNOWN` tier) stays
   **fail-closed and unreachable by any flag**.
5. No server change: tool list, annotations, authz, audit attribution, and the dispatched
   `client.call_tool` are unchanged. Classification reads the server's live annotations.

## Non-goals

- No change to the curated break-glass verbs or the `images` verbs (they stay as the ergonomic,
  argument-validated path).
- No new MCP tool, no annotation retrofit, no server-side authz/audit change.
- No interactive real-IdP login work (out of scope, deferred elsewhere).
- No change to how exit codes other than the tier-refusal (3) are derived.

## Design

### Tier classifier (`kdive.cli.passthrough`)

Replace the binary gate with a classifier over the same `ToolAnnotations` shape inspected today:

```
class ToolTier(enum.Enum):
    READ_ONLY
    MUTATING
    DESTRUCTIVE
    UNKNOWN

def classify_tool(tool: object) -> ToolTier
```

Rules (read `annotations = getattr(tool, "annotations", None)`):

| `readOnlyHint` | `destructiveHint` | tier |
|---|---|---|
| `is True` | (any) | `READ_ONLY` |
| `is False` | `is True` | `DESTRUCTIVE` |
| `is False` | not `True` (`False`/`None`/absent) | `MUTATING` |
| anything else (`None`, missing, truthy-non-`True`, no annotations, tool is `None`) | — | `UNKNOWN` |

`READ_ONLY` is decided first and dominates: a tool marked `readOnlyHint=True` is read-only even if
some future annotation also set a destructive hint (the conservative direction is to treat an
explicitly-read-only tool as read-only). Order the checks so `readOnlyHint is True` wins, then
`readOnlyHint is False` splits on `destructiveHint`, else `UNKNOWN`.

### Admission (`kdive.cli.passthrough`)

```
def assert_tool_allowed(name: str, tool: object, *, max_tier: ToolTier) -> ToolTier
```

- Compute `tier = classify_tool(tool)`.
- `UNKNOWN` → always raise `ToolNotAllowedError` (fail-closed, no flag admits it).
- Define a strict ordering `READ_ONLY < MUTATING < DESTRUCTIVE`. Admit iff `tier <= max_tier`.
- On refusal raise `ToolNotAllowedError` whose message names the tool, its tier, and the flag that
  would admit it (`--allow-mutating` for MUTATING, `--allow-destructive` for DESTRUCTIVE). Never
  name a flag for `UNKNOWN` — its message states it is not positively classified and is unreachable.
- Return the resolved `tier` so the caller knows whether to run the destructive confirmation.

`NotReadOnlyError` is removed and replaced by `ToolNotAllowedError`. `assert_read_only` is removed
(replaced by `assert_tool_allowed` with `max_tier=READ_ONLY`); no shim is kept (replace, don't
deprecate).

### Flags & dispatch (`kdive.cli.__main__`, `kdive.cli.dispatch`)

`tool call` gains three flags:

- `--allow-mutating` (store_true) → `max_tier = MUTATING`
- `--allow-destructive` (store_true) → `max_tier = DESTRUCTIVE` (implies mutating; the higher tier
  subsumes the lower — no need to pass both)
- `--yes` (store_true) → discharge the destructive confirmation without prompting

`max_tier` resolution: `DESTRUCTIVE` if `--allow-destructive` else `MUTATING` if `--allow-mutating`
else `READ_ONLY`.

`_tool_call` flow becomes:

1. Parse payload, open session, list tools.
2. `tier = assert_tool_allowed(name, tools.get(name), max_tier=max_tier)`; on
   `ToolNotAllowedError` print the message and return `_TIER_NOT_ALLOWED_EXIT` (3, the existing
   value, renamed from `_NOT_READ_ONLY_EXIT`).
3. If `tier` is `MUTATING` or `DESTRUCTIVE`: run the token-`exp` preflight (reuse
   `kdive.cli.commands.mutations.ensure_token_valid` against `session.token`); on
   `TokenExpiringError` print the message and return 3. The preflight covers both mutating tiers,
   matching the curated break-glass verbs, which preflight even the non-destructive `resources.cordon`
   / `resources.drain` (`mutations._call`) — a near-expired token can 401 mid-operation on a mutating
   call too, so an exemption is not warranted.
4. If `tier is DESTRUCTIVE`, require confirmation via `_confirm_destructive`: if `args.yes`, proceed;
   elif stdin is a TTY, prompt `type 'yes' to call <destructive tool>:` and proceed only on exact
   `yes`; else (non-TTY, no `--yes`) refuse with a message that **names `--yes`** ("destructive call
   needs confirmation: re-run with --yes for non-interactive use") and return 3.
5. `result = client.call_tool(name, arguments)`; print the envelope JSON. For an admitted call, derive
   the exit code from the envelope via `kdive.cli.errors.exit_code_for_envelope(tool_envelope(result))`
   — exactly as the curated `_run` does — so a server-side denial returned as a failure `ToolResponse`
   (the normal denial shape: `authorization_denied` from the break-glass role gate) surfaces as exit 3,
   not a silent exit 0. A clean success envelope carries no `error_category` and maps to 0, so existing
   read behavior is preserved.

The confirmation prompt and TTY check live in a small testable helper
(`_confirm_destructive(name, *, assume_yes, is_tty, prompt) -> bool`) so the decision is unit-tested
without a real terminal. Inject `input`/`isatty` via parameters or module-level seams the tests
replace.

### Affected files

- `src/kdive/cli/passthrough.py` — classifier + admission (rewrite).
- `src/kdive/cli/dispatch.py` — `_tool_call` tiered admission + destructive confirmation + preflight;
  rename exit constant.
- `src/kdive/cli/__main__.py` — add the three flags to the `tool call` subparser.
- `tests/cli/test_passthrough.py` — rewrite for the classifier + admission tiers.
- `tests/cli/test_dispatch_wiring.py` (or a new `tests/cli/test_tool_call.py`) — flag parsing +
  `_tool_call` admission/confirmation/preflight behavior with fakes.
- `docs/adr/0105-*.md`, `docs/adr/README.md`, this spec, the plan.

## Success criteria (falsifiable)

1. `classify_tool` returns `READ_ONLY/MUTATING/DESTRUCTIVE/UNKNOWN` for each of: a read-only tool, a
   `mutating()` tool, a `destructive()` tool, an unannotated object, `None`, and a truthy-non-`True`
   `readOnlyHint`. (Unit tests, one per row.)
2. `assert_tool_allowed` admits exactly the tiers `<= max_tier` and refuses the rest with
   `ToolNotAllowedError`; `UNKNOWN` is refused at every `max_tier` including `DESTRUCTIVE`.
3. Parsing `tool call x` yields `max_tier=READ_ONLY`; `--allow-mutating` → `MUTATING`;
   `--allow-destructive` → `DESTRUCTIVE`; `--allow-destructive` alone (no `--allow-mutating`) still
   admits a mutating tool.
4. `_tool_call` against a mutating tool: refused (exit 3) with no flag; dispatched (calls
   `client.call_tool`) with `--allow-mutating`; no confirmation prompt shown.
5. `_tool_call` against a destructive tool with `--allow-destructive`: prompts on a TTY and proceeds
   only on `yes`; with `--yes` proceeds without prompting; non-TTY without `--yes` refuses (exit 3),
   never calls `client.call_tool`, and the refusal message **names `--yes`**.
6. A `MUTATING` or `DESTRUCTIVE` call with an expired token is refused by the preflight (exit 3)
   before `client.call_tool`; a `READ_ONLY` call is not subject to the preflight.
7. An admitted `MUTATING`/`DESTRUCTIVE` call whose server response is an `authorization_denied`
   envelope exits 3 (derived from the envelope), not 0; an admitted call whose response is a clean
   success envelope exits 0.
8. Default `tool call somereadtool` behavior is unchanged (read tool dispatched, exit 0;
   mutating/destructive refused exit 3).
9. `just lint`, `just type`, `just test` green; the existing annotation-guard tests
   (`tests/mcp/test_read_tools_annotated.py`) still pass unchanged.

## Edge cases

- **Tool not in the server list** (`tools.get(name)` is `None`) → `classify_tool(None)` is `UNKNOWN`
  → refused at every tier. (Today this raised `NotReadOnlyError`; the refusal category is the same,
  the message differs.)
- **`--yes` without `--allow-destructive`** on a mutating tool → no effect; mutating needs only
  `--allow-mutating`; the call proceeds (no confirmation for mutating).
- **`--yes` without any tier flag** on a read tool → read tool dispatched; `--yes` is inert.
- **`--allow-mutating` on a destructive tool** → refused (mutating tier does not reach destructive),
  exit 3, message points at `--allow-destructive`.
- **Truthy-non-`True` `readOnlyHint`** (e.g. `1`) → `UNKNOWN`, never reachable — preserves the
  existing fail-closed test.
- **Confirmation read returns EOF / empty** → treated as "not yes" → refused (no dispatch).
- **Agent (non-interactive) driving a destructive tool** must pass `--allow-destructive --yes`
  together: `--allow-destructive` authorizes the tier, `--yes` discharges the otherwise-unanswerable
  confirmation. This is the documented contract for the primary (agent) caller; the non-TTY refusal
  message names `--yes` so the missing flag is self-evident.

## Review dispositions (spec adversarial review, 2026-06-13)

- **HIGH — success path returned exit 0 unconditionally, masking server denials:** `accepted-fixed`.
  The `_tool_call` flow now derives the exit code from the envelope via `exit_code_for_envelope`
  for every admitted call (step 5), matching the curated `_run`; success criterion 7 added.
- **MEDIUM — token-`exp` preflight scoped to DESTRUCTIVE only:** `accepted-fixed`. The preflight
  now covers both `MUTATING` and `DESTRUCTIVE` (step 3), matching the curated cordon/drain verbs;
  success criterion 6 covers a near-expired-token mutating call.
- **LOW — non-TTY destructive-without-`--yes` refusal under-specified for agents:** `accepted-fixed`.
  The refusal message now names `--yes` (step 4), the agent-must-pass-both contract is documented
  (edge cases), and success criterion 5 asserts the message names `--yes`.

## Out-of-scope / explicitly deferred

- Adding curated verbs for specific mutating tools.
- Any annotation changes to server tools.
- Logging/audit of the client-side opt-in (the server already audits the call; the client opt-in is
  a local gate).
