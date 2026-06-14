# Plan — Walking-skeleton end-to-end integration test (#26)

Derived from [ADR-0035](../../adr/0035-walking-skeleton-e2e-harness.md) and the M0 spec's
six exit criteria ([`m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md) §"Exit
criteria"). This is a `type:test` issue: it adds tests + fixture scripts + operator
compose tooling and changes **no** handler. Guardrails (`ruff`, `ty check src`, `pytest -m
"not live_vm"`, `shellcheck`/`shfmt`) stay green at every commit.

## Deliverables

1. `tests/integration/__init__.py` + `tests/integration/conftest.py` — re-export the
   disposable-Postgres fixtures (`migrated_url`) and the shared `_pool` / `_ctx` helpers.
2. `tests/integration/test_walking_skeleton.py` — the three non-gated exit-criterion tests
   (#3 redaction, #4 idempotency, #6 gate-refusal) **plus** the `live_vm`-gated full-path
   test (#1/#2/#5) with a fail-fast preflight.
3. `scripts/live-vm/fetch-kernel-tree.sh` + `scripts/live-vm/build-guest-image.sh` —
   reproducible fixtures (`set -euo pipefail`; shellcheck/shfmt clean; pinned inputs).
4. `docker-compose.yml` — Postgres + MinIO + a mock OIDC issuer for an operator live run.
5. `justfile` recipe `compose-up`/`compose-down` (optional convenience), and a doc note
   linking the scripts + compose to the gated test.

## Phase 1 — Non-gated exit-criterion tests (the CI signal)

These run on every PR. Each calls handlers directly with injected fakes (ADR-0019: handlers
are the unit; never MCP), using the established `_pool(migrated_url)` + `_ctx(role)` pattern
from `tests/mcp/test_control_tools.py` / `test_runs_tools.py` / `test_vmcore_tools.py`.

1a. **Gate refusal (#6).** Seed a granted Allocation + `ready` System. Drive
   `control.force_crash_system` three times, each dropping exactly one gate factor
   (capability scope / `admin` role / profile opt-in). Assert each returns
   `authorization_denied`, writes one `force_crash:denied` audit row, and enqueues **no**
   `force_crash` job. (Mirrors the existing parametrized control test, asserted here as the
   e2e criterion.)

1b. **Idempotent replay (#4).** Seed a `running` Run with a valid build profile; enqueue +
   run the `build` job via `build_handler` with a recording `_FakeBuilder`. Re-dispatch the
   *same* job; assert the builder's call count stays 1, the Run is `succeeded`, and exactly
   one `(run_id, "build")` ledger row exists. Repeat the replay assertion for `install` and
   `boot` via their handlers with recording fakes (call count unchanged on re-dispatch).

1c. **Redaction (#3).** Two sub-assertions per ADR-0035 §1:
   - *Transcript:* seed a crashed System + built Run + captured vmcore; run
     `postmortem.crash` with a `_FakeCrash` whose transcript carries a planted secret;
     assert the secret is absent and `[REDACTED]` present in `data["transcript"]`.
   - *Artifact sensitivity:* after capture, assert `artifacts.list`/`vmcore.list` return only
     the `…/vmcore-redacted` ref and `artifacts.get` on the sensitive row is not-found-shaped
     (the raw `…/vmcore` key never appears in any read response).

## Phase 2 — Fixture scripts + preflight

2a. `scripts/live-vm/fetch-kernel-tree.sh` — clone/checkout a pinned kernel ref into a target
   dir (idempotent: skip if present). `set -euo pipefail`; usage/`--help`; shellcheck +
   `shfmt -i 2` clean.

2b. `scripts/live-vm/build-guest-image.sh` — build a kdump-enabled guest image from a
   digest-pinned base into a target path (idempotent). Same shell-hygiene bar.

2c. A `live_vm` preflight helper in the integration test: read `KDIVE_GUEST_IMAGE` /
   `KDIVE_KERNEL_SRC`, and `pytest.skip` with the exact script to run when a path is absent.
   Add a `tests/scripts/` unit asserting both scripts pass `bash -n` (syntax) and start with
   the strict-mode prologue, matching the existing `tests/scripts/` convention.

## Phase 3 — Gated full-path test (the M0 exit gate)

`@pytest.mark.live_vm`, `# pragma: no cover - live_vm`. After the preflight, drive the spec
spine end-to-end against the real host, draining each async job to `succeeded` through
`Worker.run_once()` before the next dependent tool (ADR-0035 §1 queue-drive contract).
Assert: (#1) a fetchable redacted vmcore at the end; (#2) every transition + `force_crash`
wrote an `audit_log` row under the request tuple; (#5) after `allocations.release` the System
is `torn_down` **and** `Discovery.list_owned()` returns no `OwnedInfra` for the released
`system_id`. This test SKIPs in CI by design.

## Phase 4 — Operator tooling (compose) + docs

4a. `docker-compose.yml` — Postgres + MinIO + a mock OIDC issuer (e.g. a small JWKS-serving
   image), pinned by tag/digest. Not on any automated CI path (ADR-0035 §3); operator-only.

4b. A short note (in the test module docstring and/or `scripts/live-vm/README` if warranted)
   linking the scripts, compose, env vars, and `just test-live`.

## Verification

- After each phase: `uv run ruff check`, `uv run ruff format`, `uv run ty check src`,
  `uv run python -m pytest -m "not live_vm" -q` — all green, zero warnings.
- Shell: `shellcheck scripts/live-vm/*.sh` + `shfmt -i 2 -d scripts`.
- Confirm the gated test is **collected but skipped** under `-m "not live_vm"` (so a future
  un-gating is a visible diff), and that the three non-gated criterion tests actually run and
  pass against the disposable Postgres.
- Negative control: temporarily break one criterion's handler expectation to confirm the test
  fails (then revert) — the test must catch the regression it claims to.

## Rollback / cleanup

Pure additive (new files; minimal `pyproject.toml`/`uv.lock` only if a test dep is truly
needed — prefer none). Reverting the branch removes everything; no migration, no handler
change, nothing to undo in a running system.
