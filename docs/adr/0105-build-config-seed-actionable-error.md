# ADR-0105: Actionable error when the kdump build-config catalog entry is unseeded

Status: Proposed

## Context

The standard kdump build profile resolves an implicit build-config reference
`{kind: catalog, provider: system, name: "kdump"}` (`DEFAULT_CONFIG_REF`,
ADR-0096). When a `runs.build` job runs, the build worker fetches that fragment
through `build_config_fetch_from_env()._fetch`: it looks the name up in
`build_config_catalog`, fetches the object, and verifies its sha256. A missing
catalog row raises `CategorizedError(CONFIGURATION_ERROR, details={"name": name})`
with the bare message `unknown build-config catalog entry`.

The MCP tool-coverage campaign (`docs/reports/mcp-coverage-campaign-2026-06-13.md`,
finding F6 / #373) hit this on a remote-libvirt run: the build failed with that
message and the operator had no signal that the fix is to run the seed. The error
is correctly categorized but not *actionable* — it names neither the seed command
nor the missing identifier in a field an agent can act on.

Two facts constrain the fix:

1. **The seed already runs in `migrate`.** `kdive.admin.bootstrap.migrate()` calls
   `_seed_build_configs_step`, which publishes the packaged kdump fragment and
   upserts its catalog row (ADR-0096). That step is **S3-tolerant**: it skips
   cleanly when the object store is unconfigured, so a schema-only / partial
   bring-up migrate degrades instead of failing, and the fragment is seeded on a
   later `migrate` once S3 is reachable.

2. **The build path itself needs S3.** `_fetch` reads the fragment bytes from the
   object store. A build can only succeed when S3 is configured — and whenever S3
   is configured, `migrate` already seeds the row. So the only window in which the
   build hits an *unseeded* row is when the operator migrated before the object
   store was reachable (or pointed the worker at a DB that never ran the seed
   step). The campaign's "bare migrations don't seed it" was that window: a
   migrate run before S3 was wired up.

The actionable command is therefore `python -m kdive migrate` (re-run once S3 is
configured), not a new bespoke seed command.

## Decision

**Part 1 — actionable error (implemented).** When the kdump (or any) build-config
catalog entry is missing, `_fetch` raises the project's structured `CategorizedError`
with the most specific category, `CONFIGURATION_ERROR`, and carries the remediation
in the `details` payload so it survives the worker's failure-context path:

- `name`: the missing catalog name (unchanged).
- `remediation`: a literal operator command — `python -m kdive migrate` — not prose.

The worker records `CategorizedError.details` into `jobs.failure_context` as
`failure_detail_<key>` entries (`_failure_context` in `kdive.jobs.worker`), which the
job response surfaces under `data`. So `jobs.get` on the failed build returns
`data.failure_detail_remediation = "python -m kdive migrate"` and
`data.failure_detail_name = "kdump"` alongside `error_category =
"configuration_error"`. The remediation is a single committed module constant
(`SEED_REMEDIATION_COMMAND`) reused by the error so the affordance cannot drift from
the command an operator actually runs. The message string is also widened to name the
command for plain-text log readers.

**Part 2 — do not add a second seed to bare migrations.** The standard kdump
build-config stays seeded by the existing `migrate` step, which already runs on every
`migrate` and is the stock deploy path. Adding an unconditional seed to "bare
migrations" is rejected: the seed *requires* the object store (it writes the fragment
bytes), so it cannot run S3-free; a bare DDL-only migration genuinely cannot seed it,
and duplicating the existing S3-tolerant step would create two code paths that can
drift. The durable robustness fix for the campaign window is the actionable error
above plus the already-correct re-run-safe (idempotent, sha256-gated) seed in
`migrate`. The stock path (`migrate` with S3 configured → seed → build) works today;
the error now repairs the only failure window.

## Consequences

- A failed `runs.build` against an unseeded catalog now tells the operator exactly
  what to run, through a structured field (`failure_detail_remediation`) an agent can
  read, not free prose buried in a message.
- No schema change, no migration, no new operator command, no new S3 coupling. The
  build/seed S3 dependency is documented, not changed.
- The remediation command lives in one constant; a future rename of the seed command
  updates the error in one place. A test pins the affordance value, so a silent drift
  fails CI.
- `details` is the redaction-safe carrier already used by `failure_from_error`
  (`_safe_error_details`) and the worker (`_failure_context` `_safe_detail`); the
  literal command is a plain string with no secret material, so it passes both filters
  unchanged.

## Considered & rejected

- **Seed the kdump build-config from bare (DDL-only) migrations.** Rejected: the seed
  writes fragment bytes to the object store, so it cannot run without S3; a DDL-only
  migration has no store to write to. The existing S3-tolerant `migrate` seed step is
  already the correct place and already runs.

- **Carry the remediation in `suggested_next_actions` instead of `details`.** Rejected:
  a `runs.build` failure surfaces as a *job* failure. `ToolResponse.from_job` derives
  `suggested_next_actions` from the job state (`["jobs.get"]`), not from the error, and
  the worker persists only `error_category` + `failure_context`. There is no seam for a
  build worker to set a per-error `suggested_next_actions`. `details` →
  `failure_context` → response `data` is the path that actually reaches the operator.

- **Auto-seed lazily inside `_fetch` on a cache miss.** Rejected: `_fetch` runs in the
  build worker off the event loop with a short-lived sync connection and no general
  publish authority; seeding is a deploy/admin operation (ADR-0096), not a per-build
  side effect. Mixing a write into the read path would hide the missing-seed condition
  the operator needs to fix once at deploy time.

- **A dedicated `seed-build-configs` operator subcommand named in the error.** Rejected
  as redundant: `migrate` already performs this seed idempotently. Pointing the operator
  at `migrate` reuses the one command the deploy already runs rather than adding surface.
