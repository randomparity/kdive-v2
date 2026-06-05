# Async jobs

Some KDIVE operations take 30 minutes or more. Provision, build, install, and
vmcore capture run as durable jobs in a Postgres-backed queue rather than blocking
the tool call. This keeps the MCP transport responsive and makes long ops survive
worker restarts ([ADR-0008](../adr/0008-async-worker-tier-job-queue.md),
[ADR-0018](../adr/0018-job-queue-worker-execution.md)).

## The long-op pattern

A tool that starts a long operation enqueues a job and returns immediately with a
[`ToolResponse`](response-envelope.md) whose `status` is `running` (or `queued`)
and whose `object_id` is the `job_id`. The `suggested_next_actions` field at this
point contains `["jobs.wait", "jobs.cancel"]`.

The agent then polls:

- **`jobs.wait(job_id, timeout_s)`** — blocks up to `timeout_s` seconds (capped at
  300), then returns the current job envelope. Use this in preference to a manual
  poll loop.
- **`jobs.get(job_id)`** — returns the current state immediately. Use when the agent
  has other work to interleave.
- **`jobs.cancel(job_id)`** — requests cancellation. The job's declared cleanup
  contract runs; the outcome is `canceled` or `failed` depending on how far the op
  progressed.
- **`jobs.list`** — lists jobs visible to the caller, useful for triage.

When `jobs.wait` or `jobs.get` returns `status: succeeded`, the `refs` field
contains an object-store reference (e.g. `{"result": "<key>"}`) for any produced
artifact. When it returns `status: failed`, the `error_category` field names the
failure. See [errors](errors.md).

## Which operations are long-running

| Plane | Long-running tools |
|---|---|
| Allocation | `allocations.request` (when admission control defers) |
| Provisioning | `systems.provision`, `systems.reprovision`, `systems.teardown` |
| Build | `runs.build` |
| Install | `runs.install` |
| Boot | `runs.boot` |
| Control | `control.force_crash`, `control.power` |
| Retrieve | `vmcore.fetch` |

Fast operations — `debug.set_breakpoint`, `debug.read_memory`,
`debug.list_breakpoints` — are synchronous and return a `ToolResponse` directly
without a job. Note that `control.power` is **not** fast: every power action
(including `on`) enqueues a `power` job and returns a job handle.

## Durability and retries

Jobs carry a worker heartbeat/lease. If a worker dies mid-run, the job is
reclaimed by another worker for a remaining attempt. Attempt counts increment at
claim (not at failure), so a worker that dies before recording a result still
spends the attempt; jobs cannot loop forever. A job that exhausts `max_attempts`
is dead-lettered to `failed` and surfaces in `jobs.list` for triage.

Only object-store references and taxonomy categories are stored on the job row —
never raw exception messages or console text, which could carry guest output or
secret material.
