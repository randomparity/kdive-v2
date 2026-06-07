# Expected boot failures + artifact search design

- **ADR:** [ADR-0064](../../adr/0064-expected-boot-failures-artifact-search.md)
- **Motivating test case:** [`docs/test-cases/05-dcache-dhash-entries-oob-read.md`](../../test-cases/05-dcache-dhash-entries-oob-read.md)
- **Status:** Draft

## 1. Problem

The local-libvirt MCP tools are close to supporting a live agent workflow for the dcache
`dhash_entries=1` issue:

1. Build a kernel from `~/src/linux`.
2. Boot it with agent-provided debug args.
3. Observe the vulnerable crash.
4. Inspect source and patch.
5. Rebuild and boot the fixed kernel.

The missing behavior is not a runbook or script. The agent should choose MCP tools dynamically.
The MCP surface needs enough durable state and evidence access for the agent to see that a
crash was the expected reproduction signal, inspect the redacted console output, and decide the
next step.

Today, an early crash is just a boot/readiness failure. The boot handler does register the
console as a redacted artifact, but there is no first-class expected-crash outcome and no bounded
text search tool for an agent to inspect an existing artifact.

## 2. Goals

- Let a Run declare that a boot failure is expected and define the matching evidence.
- Keep unexpected boot crashes as failures.
- Record an expected crash as a successful reproduction outcome without returning full logs in
  response envelopes.
- Let agents perform bounded grep-style searches over redacted artifacts.
- Keep raw and sensitive artifacts unreachable.
- Avoid a demo driver, runbook, or dcache-specific tool.

## 3. Non-goals

- No scripted dcache workflow.
- No automatic source patch generation.
- No full artifact dump tool in this change.
- No special-case knowledge of `dhash_entries`, `__d_lookup`, or Linux 7.0 in provider code.
- No change to raw vmcore access rules.

## 4. Durable model

Add nullable `runs.expected_boot_failure jsonb`.

Domain model:

```python
class ExpectedBootFailure(BaseModel):
    kind: Literal["console_crash"]
    pattern: str
    description: str | None = None
```

Run model:

```python
class Run(...):
    ...
    expected_boot_failure: dict[str, Any] | None = None
```

The schema stores JSON so future expectation kinds can be added by a later ADR without changing
the core Run lifecycle again. The first implementation validates only `console_crash`.

Validation rules:

- `kind` must be `console_crash`.
- `pattern` must be non-empty and at most 256 characters.
- Pattern syntax is the safe artifact-search syntax described in section 6.
- `description`, if present, is at most 256 characters.

## 5. MCP tool changes

### `runs.create`

Add optional parameter:

```python
expected_boot_failure: dict[str, Any] | None = None
```

On success, persist it on the Run and include a small scalar in `data`:

```json
{
  "expected_boot_failure": "console_crash"
}
```

Malformed expectations return `configuration_error`. Cross-project, RBAC, and Run hostability
rules stay unchanged.

### `runs.get`

Expose the stored expectation in the existing string-only response data:

```json
{
  "expected_boot_failure": "console_crash",
  "expected_boot_failure_json": "{\"kind\":\"console_crash\",\"pattern\":\"__d_lookup|Oops\"}"
}
```

The agent must be able to inspect the Run intent before booting or debugging a retry. This design
does not change `ToolResponse.data` from `dict[str, str]`.

### `runs.boot`

No new parameter. The worker reads `run.expected_boot_failure`.

Suggested next actions for a boot job/result that observed an expected crash include:

```json
["artifacts.search_text", "artifacts.list", "runs.get"]
```

The exact job envelope shape remains consistent with the existing job tooling. The durable
`run_steps.boot.result` carries the boot outcome and evidence artifact id.

## 6. `artifacts.search_text`

Tool signature:

```python
artifacts.search_text(
    artifact_id: str,
    pattern: str,
    before_lines: int = 2,
    after_lines: int = 4,
    max_matches: int = 20,
) -> ToolResponse
```

Access rules:

- `artifact_id` must identify a redacted System-owned artifact row.
- Sensitive artifact ids are not-found-shaped `configuration_error`, matching `artifacts.get`.
- Run-owned artifacts are not admitted by the first implementation. They require separate
  project ownership resolution and are out of this design's search scope.
- The owning System's project must be in `ctx.projects`.
- Viewer role is required.

Bounds:

- `pattern`: 1-256 characters.
- `before_lines`: 0-10.
- `after_lines`: 0-20.
- `max_matches`: 1-50.
- Maximum searchable artifact size: 1 MiB. The tool calls `head(object_key)` first and rejects a
  larger artifact as `configuration_error` with `reason: "artifact_too_large"` before calling
  `get_artifact`.
- Maximum returned characters per line: 512.
- Maximum returned characters across `matches_json`: 64 KiB. If the cap is reached, stop adding
  context windows and set `truncated` to `"true"`.

Pattern rules:

- Treat the pattern as grep-style literal alternation: `term1|term2|term3`.
- `|` is the only operator; every term is matched as literal text.
- Empty terms are rejected.
- Reject patterns containing NUL.
- Reject patterns whose term count exceeds 16.
- Search is line-oriented over UTF-8 text with `errors="replace"`.

Response `data`:

```json
{
  "match_count": "2",
  "truncated": "false",
  "matches_json": "[{\"line\":412,\"text\":\"RIP: 0010:__d_lookup+0x...\"}]"
}
```

Response refs:

```json
{
  "artifact": "<object-store-key>"
}
```

The match windows are encoded as compact JSON under `data["matches_json"]`. A later
response-envelope ADR can improve that shape; this design does not change ADR-0019. Context
lines longer than 512 characters are clipped with a suffix marker before JSON encoding.

Match window shape:

```json
[
  {
    "line": 412,
    "text": "RIP: 0010:__d_lookup+0x...",
    "before": ["..."],
    "after": ["..."]
  }
]
```

All returned text is read from a redacted artifact. The search tool does not bypass the redactor.

Oversized artifacts:

- The first implementation does not add range reads to the object-store interface.
- A redacted artifact larger than 1 MiB is not searchable through this tool.
- The response should include `data["reason"] = "artifact_too_large"` and
  `data["size_bytes"] = "<observed size>"`.
- If later work needs tail or range search over larger logs, that work must extend the object-store
  port first rather than loading the whole object and slicing in memory.

## 7. Boot outcome flow

The worker `boot_handler` already stores a redacted console artifact in a `finally` block. This
design makes that artifact id available to the boot-result logic.

Flow:

1. `booter.boot(system_id)` succeeds.
   - Record `run_steps.boot.state = succeeded`.
   - Result includes `boot_outcome: "ready"`.
2. `booter.boot(system_id)` raises `readiness_failure`.
   - Capture and register the redacted console artifact before deciding whether to re-raise.
   - If no `expected_boot_failure`, re-raise and keep current failed-job behavior.
   - If an expectation exists, search the redacted console artifact with the expectation
     pattern.
   - If it matches, record the boot step as succeeded with:

```json
{
  "boot_outcome": "expected_crash_observed",
  "expectation_matched": true,
  "evidence_kind": "console",
  "evidence_artifact_id": "<artifact uuid>"
}
```

   - If it does not match, re-raise the original boot failure.
3. `booter.boot(system_id)` raises any other category.
   - Register the console artifact if non-empty.
   - Keep current failed-job behavior.

The worker should not mark an expected crash as reproduced if it cannot read the redacted console
artifact or if the console artifact is empty.

Implementation shape:

- Move console capture/registration into a helper that returns the redacted artifact id and object
  key, or `None` when the console is empty/unreadable.
- In `boot_handler`, catch `CategorizedError` from `booter.boot`.
- If the category is not `readiness_failure`, capture the console and re-raise.
- If the category is `readiness_failure`, capture the console, evaluate the Run expectation, and
  only suppress the exception when the expectation matches.
- When the exception is suppressed, let the job handler return normally so the job queue records a
  succeeded job, and write `run_steps.boot` as `succeeded` with `boot_outcome:
  "expected_crash_observed"`.
- When the exception is re-raised, preserve the original job failure category.

## 8. Provider gaps needed for the live demo

This design handles the workflow semantics and evidence inspection. The live dcache demo still
needs the existing local-libvirt provider stubs completed:

- Real gdbstub endpoint resolution in `src/kdive/providers/local_libvirt/connect.py`.
- Real host dump capture in `src/kdive/providers/local_libvirt/retrieve.py`.
- Real vmcore build-id extraction.
- Real redacted vmcore extraction.
- Real bounded `crash` subprocess execution.

These stay behind the current provider ports. They are not replaced by a dcache-specific demo
tool.

## 9. Tests

Unit and integration tests should cover:

- `runs.create` accepts and persists a valid `console_crash` expectation.
- `runs.create` rejects malformed expectations.
- `runs.get` exposes expected boot failure metadata.
- `boot_handler` keeps current failure behavior for unexpected crashes.
- `boot_handler` records a succeeded boot step with `expected_crash_observed` when the redacted
  console artifact matches the Run expectation.
- `boot_handler` does not claim reproduced if the expected pattern does not match.
- `boot_handler` does not claim reproduced when console capture is empty or unreadable.
- `artifacts.search_text` requires viewer role.
- `artifacts.search_text` hides sensitive artifacts as not found.
- `artifacts.search_text` returns bounded line context for redacted artifacts.
- `artifacts.search_text` rejects invalid or oversized inputs.
- `artifacts.search_text` calls object-store `head` and rejects an artifact larger than 1 MiB
  without calling `get_artifact`.

Live acceptance:

- Vulnerable Linux 7.0 with `dhash_entries=1` records `expected_crash_observed` and gives the
  agent searchable console evidence.
- Fixed kernel reaches readiness with `boot_outcome: "ready"`.

## 10. Rollout

Implement in small commits:

1. Schema/model and `runs.create` / `runs.get` metadata.
2. Shared bounded text-search helper.
3. `artifacts.search_text`.
4. Boot-handler expected-crash outcome.
5. Local-libvirt live-provider gaps.

Each step should be independently testable. The first four steps do not need a KVM host.
