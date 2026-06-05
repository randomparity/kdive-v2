# Response envelope

Every KDIVE tool returns a single `ToolResponse` defined in
`src/kdive/mcp/responses.py`. The shape is fixed across all planes so an agent
learns one envelope and one polling pattern
([ADR-0019](../adr/0019-tool-response-envelope.md)).

## Fields

| Field | Type | Meaning |
|---|---|---|
| `object_id` | `str` | The primary object this response concerns (e.g. the `job_id` for `jobs.*`, the `system_id` for `systems.*`). |
| `status` | `str` | The object's lifecycle status as a plain string (e.g. `running`, `ready`, `failed`). |
| `suggested_next_actions` | `list[str]` | Literal next **tool names** the agent should consider (e.g. `["jobs.wait", "jobs.cancel"]`). No inference needed. |
| `refs` | `dict[str, str]` | Artifact **references** keyed by role (e.g. `{"result": "<object-store-key>"}`). Never inline artifact bytes or log text. |
| `error_category` | `str \| None` | Present if and only if `status` is a failure status (`error` or `failed`). Carries a value from the `ErrorCategory` taxonomy. `None` otherwise. |
| `data` | `dict[str, str]` | Plane-specific scalars that are not one of the above (e.g. `{"kind": "provision"}` on a job response). |

## The `error_category` invariant

`error_category` is set **iff** the response reports a failure status. The model
enforces this at construction time: a failure status without a category, or any
non-failure status carrying one, raises at the tool boundary. This means a caller
can safely check `error_category is None` to distinguish success from failure
without parsing `status`.

Two distinct statuses count as a failure, and they originate differently:

- **`error`** is what a *direct tool failure* carries. The `failure()` factory
  always sets `status="error"` plus the `error_category` — so a synchronous tool
  rejection (bad input, authorization denied, sequencing error) is always `error`,
  never `failed`.
- **`failed`** is a *job terminal state*. It appears only on job-handle envelopes
  built from a `Job` row (via `from_job`), surfaced through `jobs.get` / `jobs.wait`
  when a long-running operation fails. A direct tool call never returns `failed`.

See [errors](errors.md) for the taxonomy and recovery guidance.

## References, not log dumps

The `refs` field carries object-store keys, not raw artifact bytes or console
transcripts. The `data` field is constrained to `str → str` scalars. There is no
field for inline log text — this is structural: a tool cannot accidentally return
a raw transcript or vmcore dump in the envelope. Artifact bytes are fetched
separately via `artifacts.get` after the agent inspects the reference.

All guest output, gdb/SoL transcripts, and console logs pass through the redactor
before persistence and before any response snippet. See [safety and RBAC](safety-and-rbac.md)
for the redaction contract.

## List responses

`*.list` tools return a sequence of `ToolResponse` objects, one envelope per item.
Batch callers isolate construction per item so a single failed row does not blank
the whole list.
