# Accounting assertions + report artifact Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Append a `report` phase to the live-stack spine that drives `accounting.report`'s all-projects form under a `platform_auditor` token, asserts this run's windowed spend against the ledger, emits a JSON report artifact, and adds a wire RBAC negative — all `live_stack`-gated, skipping cleanly with no stack.

**Architecture:** A test-only addition to `tests/integration/test_live_stack.py` (the merged #100 spine). New module-level helpers (`_seed_metering`, `_db_now`, `_ledger_sums`, `_report_artifact_dir`, `_write_report_artifact`, `_find_project_row`) plus the `report` phase inside `test_spine_over_the_wire`, and a standalone `live_stack` RBAC-negative test. No product code changes.

**Tech Stack:** Python 3.13, pytest, psycopg (async), the merged `LiveStackClient`/`mint_token` harness, `kdive.domain.cost.quantize_kcu`.

---

## File structure

- **Modify/Test:** `tests/integration/test_live_stack.py` — add helpers, the `report` phase, and the RBAC-negative test. This is the only file touched. All additions are appended next to D's existing helpers; D's phases are not modified beyond inserting the `report` phase after `teardown` and capturing the window `start` at `allocate`.

The whole change is test code; guardrails are `just lint` / `just type` / `just test` and a clean skip from `just test-live-stack`. There is no failing-then-passing unit-test cycle here because the new tests are `live_stack`-gated and skip without a stack — verification is (a) collection + clean skip, (b) lint/type green, (c) the non-live suite stays green, (d) the two non-gated `phase`-contract unit tests still pass.

---

### Task 1: Metering seed + DB-clock + ledger-sum helpers

**Files:**
- Modify: `tests/integration/test_live_stack.py` (add helpers after `_grant_force_crash_scope`, near line 150)

- [ ] **Step 1: Add the imports**

At the top of the file, ensure `Decimal` and `datetime`/`UTC` and `quantize_kcu` are imported. Add to the existing import block:

```python
import json
import tempfile
from datetime import UTC, datetime
from decimal import Decimal

from kdive.domain.cost import quantize_kcu
```

(`os`, `asyncio`, `time`, `Path`, `psycopg`, `pytest` are already imported. `json` may already be imported — do not duplicate; check first.)

- [ ] **Step 2: Add the seed + clock + ledger-sum helpers**

After `_grant_force_crash_scope` (around line 150), add:

```python
# Admission is fail-closed on metering (ADR-0007 §4): _within_budget and _within_alloc_quota
# both deny a project with no row, writing no ledger row. The spine never sets a budget over
# the wire, so seed both out of band before allocate (mirrors _grant_force_crash_scope), or
# the report phase has no spend to assert (ADR-0046 §0).
_SEED_LIMIT_KCU = "1000000"
_SEED_MAX_ALLOCATIONS = 4
_SEED_MAX_SYSTEMS = 4


async def _seed_metering(db_url: str, project: str) -> None:
    """Seed the budget (limit-only) + quota rows admission requires, out of band.

    The budget upsert writes ``limit_kcu`` only and leaves ``spent_kcu`` untouched (matching
    production ``set_budget`` / ``BUDGETS.upsert``), so a re-run of the fixed-constant project
    keeps the DB-maintained running total consistent with the ledger Σ; a first insert starts
    it at 0. Both upserts are idempotent on the ``project`` primary key.
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.execute(
            "INSERT INTO budgets (project, limit_kcu) VALUES (%s, %s) "
            "ON CONFLICT (project) DO UPDATE SET limit_kcu = EXCLUDED.limit_kcu",
            (project, _SEED_LIMIT_KCU),
        )
        await conn.execute(
            "INSERT INTO quotas (project, max_concurrent_allocations, max_concurrent_systems) "
            "VALUES (%s, %s, %s) ON CONFLICT (project) DO UPDATE SET "
            "max_concurrent_allocations = EXCLUDED.max_concurrent_allocations, "
            "max_concurrent_systems = EXCLUDED.max_concurrent_systems",
            (project, _SEED_MAX_ALLOCATIONS, _SEED_MAX_SYSTEMS),
        )
        await conn.commit()


async def _db_now(db_url: str) -> datetime:
    """Read the Postgres server clock, so the report window shares one clock with ledger.ts."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute("SELECT now()")
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT now() returned no row")
    return row[0]


async def _ledger_sums(db_url: str, project: str, since: datetime) -> tuple[Decimal, Decimal]:
    """Return ``(reserved, reconciled)`` ledger kcu_delta sums for ``project`` over ``ts >= since``.

    Quantized via the domain ``quantize_kcu`` so the DB cross-check compares like-for-like with
    the wire rollup (which the tool also quantizes).
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT "
            "COALESCE(SUM(kcu_delta) FILTER (WHERE event_type = 'reserved'), 0), "
            "COALESCE(SUM(kcu_delta) FILTER (WHERE event_type = 'reconciled'), 0) "
            "FROM ledger WHERE project = %s AND ts >= %s",
            (project, since),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("ledger sum query returned no row")
    return quantize_kcu(Decimal(row[0])), quantize_kcu(Decimal(row[1]))
```

- [ ] **Step 3: Verify it collects and lints**

Run: `just lint && just type`
Expected: PASS (zero warnings). The helpers are not yet referenced — `ty` may flag unused; they are used in Task 3, so finish Task 1–3 before judging unused-import warnings. If lint flags an unused import in isolation, proceed to Task 2/3 which use them, then re-run.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_live_stack.py
git commit -m "test: add metering-seed + DB-clock + ledger-sum helpers (#101)"
```

---

### Task 2: Artifact helpers + project-row locator

**Files:**
- Modify: `tests/integration/test_live_stack.py` (add after the Task 1 helpers)

- [ ] **Step 1: Add the artifact + row helpers**

```python
_ARTIFACT_DIR_ENV = "KDIVE_ARTIFACT_DIR"
_ARTIFACT_NAME = "accounting-report.json"


def _report_artifact_dir() -> Path:
    """Resolve the artifact dir: ``KDIVE_ARTIFACT_DIR`` or an out-of-tree temp default.

    The default lives under ``tempfile.gettempdir()`` (never inside the repo) so a live run
    does not dirty the working tree or get walked by whole-tree tooling (ADR-0046 §3).
    """
    override = os.environ.get(_ARTIFACT_DIR_ENV)
    base = Path(override) if override else Path(tempfile.gettempdir()) / "kdive-live-stack-artifacts"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _write_report_artifact(payload: dict[str, object]) -> Path:
    """Write the report payload as ``accounting-report.json``; return its path."""
    path = _report_artifact_dir() / _ARTIFACT_NAME
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def _find_project_row(rows: list[dict[str, object]], project: str) -> dict[str, object]:
    """Return the rollup row for ``project``, or fail the phase if absent (no spend rolled up)."""
    for row in rows:
        if row.get("project") == project:
            return row
    raise AssertionError(f"no rollup row for project {project!r} (no spend in the window?)")
```

- [ ] **Step 2: Verify lint + type**

Run: `just lint && just type`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_live_stack.py
git commit -m "test: add report artifact + rollup-row helpers (#101)"
```

---

### Task 3: Wire the `report` phase + window into the spine

**Files:**
- Modify: `tests/integration/test_live_stack.py` — `test_spine_over_the_wire` (around lines 297-395) and `_assert_audit`/`_assert_teardown` follow it

- [ ] **Step 1: Seed metering + capture the window at `allocate`**

In `test_spine_over_the_wire`, mint the auditor token alongside the existing tokens and seed metering + capture the window before the `allocate` phase. Replace the token setup and the allocate block opening:

Find:
```python
    issuer, base_url, db_url = _spine_preflight()
    operator_token = _token(issuer, role="operator")
    admin_token = _token(issuer, role="admin")
```
Replace with:
```python
    issuer, base_url, db_url = _spine_preflight()
    operator_token = _token(issuer, role="operator")
    admin_token = _token(issuer, role="admin")
    auditor_token = _token(issuer, role="viewer", platform_roles=["platform_auditor"])
```

Then, inside `_run`, immediately after `async with op, admin:` and before `async with phase("allocate"):`, seed metering and capture the window start from the DB clock:
```python
        async with op, admin:
            await _seed_metering(db_url, _PROJECT)  # ADR-0046 §0: admission is fail-closed
            window_start = await _db_now(db_url)  # window bound shares ledger.ts's clock
            async with phase("allocate"):
```

- [ ] **Step 2: Add the `report` phase after `teardown`**

Find the teardown phase (the last phase in the `async with op, admin:` block):
```python
            async with phase("teardown"):  # reconciler-driven (≥30s) → torn_down
                await _await_system_state(op, "teardown", system_id, "torn_down")
```
Add immediately after it (still inside the `async with op, admin:` block):
```python
            async with phase("report"):
                await _assert_report(base_url, auditor_token, db_url, window_start)
```

- [ ] **Step 3: Add the `_assert_report` helper**

After `_assert_teardown` (end of file), add:

```python
async def _assert_report(
    base_url: str, auditor_token: str, db_url: str, window_start: datetime
) -> None:
    """Drive accounting.report (all-projects) under platform_auditor; assert windowed spend.

    Asserts the _PROJECT rollup row reflects this run's real spend (windowed wire rollup ==
    windowed DB ledger sums), then emits + re-asserts the JSON report artifact (ADR-0046 §2/§3).
    """
    auditor = LiveStackClient.over_http(base_url, auditor_token)
    async with auditor:
        env = await _scalar(
            auditor,
            "accounting.report",
            scope="all-projects",
            window=[window_start.isoformat(), None],
        )
    _ok(env, "report")
    rows = json.loads(str(env.data["rows"]))
    total = json.loads(str(env.data["total"]))
    row = _find_project_row(rows, _PROJECT)
    reserved = Decimal(str(row["reserved"]))
    reconciled = Decimal(str(row["reconciled"]))
    variance = Decimal(str(row["variance"]))
    assert reserved > 0, "report shows no reserved spend for the run (#101)"
    assert variance == reconciled - reserved, "report variance != reconciled - reserved (#101)"
    db_reserved, db_reconciled = await _ledger_sums(db_url, _PROJECT, window_start)
    assert reserved == db_reserved, f"wire reserved {reserved} != DB {db_reserved} (#101)"
    assert reconciled == db_reconciled, f"wire reconciled {reconciled} != DB {db_reconciled} (#101)"
    artifact = _write_report_artifact(
        {"scope": env.data["scope"], "window": [window_start.isoformat(), None],
         "project_row": row, "total": total}
    )
    assert artifact.exists(), f"report artifact not written at {artifact} (#101)"
    written = json.loads(artifact.read_text())
    assert Decimal(str(written["project_row"]["reserved"])) == reserved, "artifact reserved drift"
    assert Decimal(str(written["project_row"]["reconciled"])) == reconciled, "artifact reconciled drift"
    assert Decimal(str(written["project_row"]["variance"])) == variance, "artifact variance drift"
```

- [ ] **Step 4: Verify the non-live suite + skip path**

Run: `just lint && just type && just test`
Expected: PASS, zero warnings. The two non-gated `phase`-contract unit tests still pass; the `live_stack` tests are deselected.

Run: `just test-live-stack`
Expected: clean skip — "no live_stack tests collected" OR all `live_stack` tests `SKIPPED` (preflight: no `KDIVE_GUEST_IMAGE`/stack). Exit 0.

- [ ] **Step 5: Commit (separate commit from D — the report phase)**

```bash
git add tests/integration/test_live_stack.py
git commit -m "test: drive report phase + windowed spend assertions (#101)"
```

---

### Task 4: The wire RBAC negative

**Files:**
- Modify: `tests/integration/test_live_stack.py` — add a standalone `live_stack` test near `test_viewer_denied_operator_op_over_the_wire`

- [ ] **Step 1: Add the negative test**

After `test_viewer_denied_operator_op_over_the_wire` (around line 291), add:

```python
@pytest.mark.live_stack
def test_report_all_projects_denied_to_project_token() -> None:
    """A project-only token is denied accounting.report's all-projects form over the wire.

    Verified against the tool: the all-projects form catches the raised AuthorizationError and
    *returns* ToolResponse.failure(..., AUTHORIZATION_DENIED) — a well-formed error envelope,
    not a raised tool error. So assert the envelope shape (like crash-rbac-negative), not a
    raised LiveStackToolError (ADR-0046 §3).
    """
    issuer, base_url, _db = _spine_preflight()

    async def _run() -> None:
        project_only = LiveStackClient.over_http(base_url, _token(issuer, role="viewer"))
        async with project_only:
            denied = await _scalar(project_only, "accounting.report", scope="all-projects")
        assert denied.status == "error", "project-only token was not denied (#101)"
        assert denied.error_category == "authorization_denied", "wrong denial category (#101)"

    asyncio.run(_run())
```

- [ ] **Step 2: Verify**

Run: `just lint && just type && just test`
Expected: PASS, zero warnings.

Run: `just test-live-stack`
Expected: clean skip (preflight). Exit 0.

- [ ] **Step 3: Update the module docstring**

The module docstring (lines 1-20) enumerates the asserted criteria. Append a sentence noting the report phase: the windowed `accounting.report` all-projects spend assertion + artifact, and the project-only `authorization_denied` envelope negative. Keep it factual and short.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_live_stack.py
git commit -m "test: wire RBAC negative for all-projects report (#101)"
```

---

## Self-review

- **Spec coverage:** Metering seed (Task 1) → spec "Metering prerequisite"; DB-clock window (Task 1 `_db_now` + Task 3 capture) → spec §2; ledger cross-check (Task 1 `_ledger_sums` + Task 3) → spec step 5; artifact out-of-tree (Task 2 + Task 3) → spec step 6; report phase (Task 3) → spec "report phase"; RBAC negative (Task 4) → spec "wire RBAC negative". All mapped.
- **Placeholders:** none — every step shows the literal code/command.
- **Type consistency:** `_db_now -> datetime`, `_ledger_sums(db_url, project, since: datetime) -> tuple[Decimal, Decimal]`, `_seed_metering(db_url, project) -> None`, `_write_report_artifact(payload) -> Path`, `_find_project_row(rows, project) -> dict`. `_assert_report(base_url, auditor_token, db_url, window_start)` uses all consistently. `window_start` is a server-clock `datetime`; `.isoformat()` is tz-aware (Postgres `now()` returns tz-aware), satisfying the tool's `_parse_window` tz check.
- **Separate-from-D:** Task 3/4 are the report phase + negative, committed apart from any D change; D's phases are unmodified except the appended `report` phase and the `window_start`/seed capture at the top of the existing block.
