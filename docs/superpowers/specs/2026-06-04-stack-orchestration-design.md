# Stack orchestration + runbook (M1.2 sub-issue C) — design

- **Issue:** [#99](https://github.com/randomparity/kdive/issues/99) (M1.2 epic [#95](https://github.com/randomparity/kdive/issues/95), sub-issue C)
- **Decision anchor:** [ADR-0042 §2](../../adr/0042-live-stack-e2e-mcp-http.md) (host-run processes against containerized backends)
- **Umbrella spec:** [`2026-06-04-live-stack-e2e-design.md`](2026-06-04-live-stack-e2e-design.md)
- **Plan:** [`docs/plans/m1.2-implementation.md`](../../plans/m1.2-implementation.md) Phase B / Issue C

## Goal

A one-command live-stack bring-up and the operator runbook, plus the `live_stack`
pytest-marker registration and skip-plumbing so `just test-live-stack` exists and
**skips cleanly** when the stack or fixtures are absent — even before sub-issue D
([#100](https://github.com/randomparity/kdive/issues/100)) lands the spine test that
carries the marker.

This is ops/docs only. No `src/` changes. The existing `docker-compose.yml` backends
(Postgres + MinIO + `kdive-artifacts` bucket + mock-oauth2-server) are reused unchanged;
this issue wires env and process startup around them, it does not rewrite the stack.

## Scope boundary with #100 (sub-issue D)

| owned here (#99) | owned by #100 (D) |
|------------------|-------------------|
| `live_stack` marker registration in `pyproject.toml` | the `test_live_stack.py` spine driver body |
| `just stack-up` / `just test-live-stack` recipes | the per-phase tool calls + assertions |
| `docs/runbooks/live-stack.md` + `AGENTS.md` pointer | deletion of `test_walking_skeleton_full_path` |
| the env-var contract for host `server`/`worker`/`reconciler` | — |
| the clean-skip mechanism for an empty `live_stack` suite | the preflight skip *inside* the driver |

This issue does **not** add any `live_stack`-marked test. The driver that carries the
marker is #100's. The clean-skip behaviour must therefore hold when **zero** tests carry
the marker (today) and continue to hold once #100's preflight-guarded driver lands and
skips itself when fixtures/stack are absent.

## The clean-skip mechanism

Observed pytest exit codes (pytest 9.0.3, run in this worktree) for `-m live_stack`
with no test carrying the marker:

| invocation | exit | meaning |
|------------|------|---------|
| `pytest -m live_stack` (no marked test) | **5** | "no tests ran" — all 1298 deselected |
| `pytest -m live_stak` (typo'd marker **name**) | **5** | also "no tests ran" — *indistinguishable from above* |
| `pytest -m "live_stack and"` (malformed expr) | **4** | usage error |
| `pytest -m live_stack` (driver present, preflight skips) | **0** | 1 collected, 1 skipped |

A bare recipe would surface exit 5 as a recipe failure, contradicting "skips cleanly."
`test-live-stack` therefore treats exit **5 (no tests collected)** as a clean skip and
**propagates every other non-zero code** (4 usage error, 1 test failure, 2 interrupt,
3 internal error) unchanged. This is the standard pytest idiom for an optional suite
whose tests may all be deselected:

- **today (no marked test):** exit 5 → recipe prints a "no live_stack tests collected"
  note and succeeds.
- **after #100 lands, fixtures/stack absent:** the driver's preflight `pytest.skip`s →
  the suite collects 1 test, skips it → exit 0 → recipe succeeds.
- **after #100 lands, stack present:** the driver runs → exit 0/1 as normal.

**The exit-5 ambiguity and its guard.** Exit 5 cannot distinguish "no test carries the
marker yet" (the intended state) from "a test *meant* to carry the marker is mis-marked
or the marker was renamed" (a regression that would make the recipe a silent no-op
forever). Two guards bound this:

1. **`--strict-markers`** in the recipe: an `@pytest.mark.<typo>` on a test fails
   collection (exit ≠5) instead of silently deselecting, so #100 cannot mis-mark its
   driver without the recipe going red. (This validates markers *on tests*; it does not
   catch a typo in the recipe's own `-m` expression — see guard 2.)
2. **The recipe echoes pytest's own summary line** (`N deselected` / `N skipped`) before
   reporting "skipped cleanly," so a zero-collection run is *visibly* a skip, not an
   opaque green. An operator reading "1298 deselected, 0 selected" sees the suite ran and
   matched nothing; "1 skipped" shows the driver self-skipped. The recipe's `-m`
   expression is a fixed literal in the committed `justfile`, reviewed once — it is not
   operator input — so the residual typo risk is a one-time review item, not a runtime
   hazard.

Exit-5-as-skip is scoped to *this* recipe; `just test` and `just test-live` keep their
default semantics.

### Considered & rejected

- **Seed a placeholder `live_stack` test here.** A throwaway test that always skips would
  make exit 0 without special-casing 5, but #100 must then delete or collide with it, and
  it duplicates the preflight #100 owns. Rejected: the marker + a 5-tolerant recipe is the
  smaller, collision-free seam.
- **`pytest ... || true` in the recipe.** Swallows *every* failure (exit 1 test failure,
  exit 4 usage error, exit 2/3) — not just exit 5. Rejected: only exit 5 is "clean"; all
  other non-zero codes must propagate.
- **Drop `-q`/use `--co` collection-only.** Collection-only never runs the driver, so it
  can't be the suite-runner. Rejected.

## Env-var contract (host `server` / `worker` / `reconciler`)

All three host processes read the same `KDIVE_*` env, pointed at the compose-published
host ports. The contract (sourced from `docker-compose.yml` and the consuming modules
`db/pool.py`, `mcp/auth.py`, `store/objectstore.py`):

| var | value (default compose) | consumed by |
|-----|-------------------------|-------------|
| `KDIVE_DATABASE_URL` | `postgresql://kdive:kdive@localhost:5432/kdive` | `db/pool.py` |
| `KDIVE_OIDC_ISSUER` | `http://localhost:8090/default` | `mcp/auth.py` |
| `KDIVE_OIDC_JWKS_URI` | `http://localhost:8090/default/jwks` | `mcp/auth.py` |
| `KDIVE_OIDC_AUDIENCE` | `kdive` | `mcp/auth.py` |
| `KDIVE_S3_ENDPOINT_URL` | `http://localhost:9000` | `store/objectstore.py` |
| `KDIVE_S3_BUCKET` | `kdive-artifacts` | `store/objectstore.py` |
| `KDIVE_S3_REGION` | `us-east-1` | `store/objectstore.py` |
| `AWS_ACCESS_KEY_ID` | `minioadmin` | boto3 default chain (MinIO root user) |
| `AWS_SECRET_ACCESS_KEY` | `minioadmin` | boto3 default chain (MinIO root password) |
| `KDIVE_HTTP_HOST` / `KDIVE_HTTP_PORT` | `127.0.0.1` / `8000` | `__main__.py` server |

`store/objectstore.py` takes S3 **credentials from boto3's default chain** (`AWS_*`), not
`KDIVE_S3_*` — so MinIO's `minioadmin`/`minioadmin` must be exported as `AWS_ACCESS_KEY_ID`
/`AWS_SECRET_ACCESS_KEY`. This is the one non-obvious wiring detail and is the runbook's
most error-prone step.

The driver additionally needs `KDIVE_STACK_BASE_URL` (e.g. `http://127.0.0.1:8000/mcp/`)
to reach the running server over the wire, and the VM fixtures `KDIVE_GUEST_IMAGE` /
`KDIVE_KERNEL_SRC` — these are inputs to #100's preflight, documented in the runbook so
the operator sets them before `just test-live-stack`.

### `stack-up` ordering

`stack-up` brings up the **backends only** (`docker compose up -d --wait`) and prints the
env-export block + the next steps. The **`--wait`** flag is load-bearing: plain
`up -d` returns once containers are *created*, not *healthy*, so an operator who then
immediately starts `server` would hit connection-refused against a still-starting
Postgres/MinIO and misread a timing race as a code bug. `--wait` blocks on the
compose healthchecks (Postgres `pg_isready`, MinIO `mc ready`) and on `minio-init`
exiting, so a successful `stack-up` means the backends are actually reachable. It does
**not** fork the host `server`/`worker`/`reconciler`: those are long-lived foreground
processes an operator runs in separate terminals (or under a supervisor), and a `just`
recipe that backgrounded three processes would own a lifecycle it cannot cleanly reap
within `set -euo pipefail`. The runbook gives the exact three commands. This keeps
`stack-up` idempotent and re-runnable, and leaves process supervision to the operator —
matching ADR-0042 §2's "run the processes on the host" and the deferred-containerization
posture (sub-issue F owns a supervised topology).

## Acceptance

- A documented one-command backend bring-up (`just stack-up`) with the env block and the
  three host-process commands in the runbook.
- `just test-live-stack` runs the `live_stack` suite and **skips cleanly** when
  fixtures/stack are absent — verified by running it with no stack present and observing
  both a zero exit (exit 5 tolerated) **and** pytest's `… deselected`/`… skipped` summary
  in the output.
- `live_stack` marker registered in `pyproject.toml`; `AGENTS.md` points at the runbook.

## Known risk: stale compose image tag

The committed `docker-compose.yml` pins `ghcr.io/navikt/mock-oauth2-server:3.1.4`, which
no longer resolves on ghcr.io (`not found`) as of this work, so a real `stack-up` cannot
pull the OIDC issuer today. This is a pre-existing defect in the compose file, **not**
introduced or owned by this issue (which wires env around the existing backends and does
not re-pin images). It blocks an end-to-end `stack-up` smoke but **not** this issue's
acceptance: the clean-skip path (`test-live-stack` with no stack) needs no containers.
Flagged for a follow-up image re-pin; the runbook notes the operator must have pullable
images.

## Verification

- `just --list` shows `stack-up` and `test-live-stack`.
- `just test-live-stack` with no stack/marked test → **exit 0**, and the output shows
  pytest's `… deselected` summary plus the recipe's "no live_stack tests collected" note
  (a positive, output-based signal that the suite ran and matched nothing — not an opaque
  green). This is the acceptance-critical path and needs no running stack.
- `shellcheck`/`shfmt -d` clean on the recipe shell; `just lint`/`just type`/`just test`
  green (no `src/` change, so these are unaffected but must stay green).
