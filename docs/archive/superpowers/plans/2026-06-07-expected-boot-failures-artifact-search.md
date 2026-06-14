# Expected Boot Failures + Artifact Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Run-scoped expected boot failure metadata, expected-crash boot outcomes, and a
bounded `artifacts.search_text` tool over redacted System-owned artifacts.

**Architecture:** Store expected boot failure intent on the durable Run row. Keep boot evidence
artifact-backed: `runs.boot` records a small structured outcome and an evidence artifact id, while
agents inspect redacted console output through a bounded literal text-search helper shared by the
worker and MCP artifact tool.

**Tech Stack:** Python 3.13, Pydantic, psycopg, FastMCP, PostgreSQL JSONB migrations, MinIO/S3
object-store adapter, `uv`, `ruff`, `ty`, `pytest`.

---

## File Map

- Create `src/kdive/db/schema/0008_expected_boot_failure.sql`
  - Adds nullable `runs.expected_boot_failure jsonb` with a null-or-object check.
- Modify `src/kdive/domain/models.py`
  - Adds `ExpectedBootFailure` Pydantic model.
  - Adds `Run.expected_boot_failure`.
- Modify `src/kdive/db/repositories.py`
  - Adds `expected_boot_failure` to `RUNS` JSON columns.
- Modify `src/kdive/mcp/tools/lifecycle/runs/create.py`
  - Accepts and validates optional `expected_boot_failure`.
  - Persists it on new Runs.
- Modify `src/kdive/mcp/tools/lifecycle/runs/common.py`
  - Exposes expected boot failure metadata in `runs.get` envelopes.
- Create `src/kdive/security/artifact_search.py`
  - Implements bounded literal OR parsing and line-context search.
- Modify `src/kdive/mcp/tools/catalog/artifacts.py`
  - Adds `artifacts.search_text`.
  - Uses object-store `head()` before `get_artifact()`.
  - Keeps System-owned redacted-only access rules.
- Modify `src/kdive/jobs/handlers/runs.py`
  - Refactors console capture into a helper returning artifact id/key.
  - Suppresses `readiness_failure` only when Run expectation matches redacted console evidence.
- Modify `src/kdive/mcp/tools/debug/sessions.py`
  - Rejects live debug attach for Runs whose boot ledger recorded `expected_crash_observed`.
- Modify `docs/guide/reference/artifacts.md`
  - Documents `artifacts.search_text`.
- Modify `docs/guide/reference/index.md`
  - Adds `artifacts.search_text` to the reference table.
- Tests:
  - `tests/domain/test_models.py`
  - `tests/db/test_migrate.py`
  - `tests/mcp/lifecycle/test_runs_tools.py`
  - `tests/mcp/catalog/test_artifacts_tools.py`
  - `tests/mcp/debug/test_debug_tools.py`
  - New `tests/security/test_artifact_search.py`

---

### Task 1: Schema And Run Model

**Files:**
- Create: `src/kdive/db/schema/0008_expected_boot_failure.sql`
- Modify: `src/kdive/domain/models.py`
- Modify: `src/kdive/db/repositories.py`
- Test: `tests/db/test_migrate.py`
- Test: `tests/domain/test_models.py`

- [ ] **Step 1: Write failing migration/model tests**

Add this test to `tests/db/test_migrate.py`:

```python
def test_runs_expected_boot_failure_column(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    columns = _columns(pg_conn, "runs")
    assert columns["expected_boot_failure"] == "jsonb"
```

Add this helper near the other schema helpers:

```python
def _columns(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    ).fetchall()
    return {str(name): str(data_type) for name, data_type in rows}
```

Add this test to `tests/domain/test_models.py`:

```python
def test_expected_boot_failure_model_and_run_field() -> None:
    expected = ExpectedBootFailure(
        kind="console_crash",
        pattern="__d_lookup|Oops",
        description="dcache one-bucket hash crash",
    )
    run = Run(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="p",
        project="proj",
        investigation_id=uuid4(),
        system_id=uuid4(),
        state=RunState.CREATED,
        build_profile={"source": "server"},
        expected_boot_failure=expected.model_dump(mode="json"),
    )
    assert run.expected_boot_failure == {
        "kind": "console_crash",
        "pattern": "__d_lookup|Oops",
        "description": "dcache one-bucket hash crash",
    }
```

Update the imports in `tests/domain/test_models.py`:

```python
from kdive.domain.models import ExpectedBootFailure
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run python -m pytest tests/db/test_migrate.py::test_runs_expected_boot_failure_column tests/domain/test_models.py::test_expected_boot_failure_model_and_run_field -q
```

Expected:

- migration test fails because `expected_boot_failure` is absent.
- model test fails because `ExpectedBootFailure` is undefined or `Run` rejects the field.

- [ ] **Step 3: Add migration**

Create `src/kdive/db/schema/0008_expected_boot_failure.sql`:

```sql
-- ADR-0064: Run-scoped expected boot failure metadata for expected-crash reproduction.

ALTER TABLE runs
    ADD COLUMN expected_boot_failure jsonb,
    ADD CONSTRAINT runs_expected_boot_failure_object_check
        CHECK (
            expected_boot_failure IS NULL
            OR jsonb_typeof(expected_boot_failure) = 'object'
        );
```

- [ ] **Step 4: Update migration version expectation**

In `tests/db/test_migrate.py`, update `test_rerun_is_a_noop`:

```python
def test_rerun_is_a_noop(pg_conn: psycopg.Connection) -> None:
    first = migrate.apply_migrations(pg_conn)
    second = migrate.apply_migrations(pg_conn)
    assert first == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"]
    assert second == []
```

- [ ] **Step 5: Add domain model and Run field**

In `src/kdive/domain/models.py`, add imports:

```python
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Any, Literal, TypedDict
```

If `BaseModel`, `ConfigDict`, `Field`, `Any`, and `TypedDict` already exist, only add
`field_validator` and `Literal`.

Add this model near the other Run-related models:

```python
class ExpectedBootFailure(_DomainBase):
    """Run-scoped expected boot failure metadata (ADR-0064)."""

    kind: Literal["console_crash"]
    pattern: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=256)

    @field_validator("pattern")
    @classmethod
    def _literal_or_pattern(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("pattern must not contain NUL")
        terms = value.split("|")
        if any(term == "" for term in terms):
            raise ValueError("pattern contains an empty term")
        if len(terms) > 16:
            raise ValueError("pattern has too many terms")
        return value
```

Add the Run field:

```python
class Run(DomainModel, _Attribution):
    """One build/install/boot attempt — the join of a System and an Investigation."""

    investigation_id: UUID
    system_id: UUID
    state: RunState
    build_profile: dict[str, Any]
    expected_boot_failure: dict[str, Any] | None = None
    kernel_ref: str | None = None
    debuginfo_ref: str | None = None
    failure_category: ErrorCategory | None = None
```

- [ ] **Step 6: Add repository JSON column**

Change `RUNS` in `src/kdive/db/repositories.py`:

```python
RUNS = StatefulRepository(
    Run,
    "runs",
    RunState,
    json_columns=frozenset({"build_profile", "expected_boot_failure"}),
)
```

- [ ] **Step 7: Run tests to verify they pass**

Run:

```bash
uv run python -m pytest tests/db/test_migrate.py::test_runs_expected_boot_failure_column tests/db/test_migrate.py::test_rerun_is_a_noop tests/domain/test_models.py::test_expected_boot_failure_model_and_run_field -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/kdive/db/schema/0008_expected_boot_failure.sql src/kdive/domain/models.py src/kdive/db/repositories.py tests/db/test_migrate.py tests/domain/test_models.py
git commit -m "feat: add expected boot failure run metadata"
```

---

### Task 2: Run Create/Get Tool Surface

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/create.py`
- Modify: `src/kdive/mcp/tools/lifecycle/runs/common.py`
- Test: `tests/mcp/lifecycle/test_runs_tools.py`

- [ ] **Step 1: Write failing `runs.create` and `runs.get` tests**

Add these tests near the existing create/get tests in `tests/mcp/lifecycle/test_runs_tools.py`:

```python
def test_create_run_persists_expected_boot_failure(migrated_url: str) -> None:
    expected = {
        "kind": "console_crash",
        "pattern": "__d_lookup|Oops",
        "description": "dcache crash",
    }

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await runs_tools.create_run(
                pool,
                _ctx(),
                investigation_id=inv_id,
                system_id=sys_id,
                build_profile=_profile(),
                expected_boot_failure=expected,
            )
            assert resp.status == "created"
            assert resp.data["expected_boot_failure"] == "console_crash"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT expected_boot_failure FROM runs WHERE id = %s", (resp.object_id,)
                )
                row = await cur.fetchone()
            assert row is not None
            assert row["expected_boot_failure"] == expected

    asyncio.run(_run())


def test_create_run_rejects_bad_expected_boot_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, state=InvestigationState.OPEN)
            sys_id = await _seed_system(pool)
            resp = await runs_tools.create_run(
                pool,
                _ctx(),
                investigation_id=inv_id,
                system_id=sys_id,
                build_profile=_profile(),
                expected_boot_failure={"kind": "console_crash", "pattern": ""},
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM runs")
                row = await cur.fetchone()
            assert row is not None and row["n"] == 0

    asyncio.run(_run())


def test_get_run_exposes_expected_boot_failure(migrated_url: str) -> None:
    expected = {"kind": "console_crash", "pattern": "__d_lookup|Oops"}

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, state=RunState.CREATED)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE runs SET expected_boot_failure = %s WHERE id = %s",
                    (Jsonb(expected), run_id),
                )
            resp = await runs_tools.get_run(pool, _ctx(), run_id)
        assert resp.data["expected_boot_failure"] == "console_crash"
        assert json.loads(resp.data["expected_boot_failure_json"]) == expected

    asyncio.run(_run())
```

Add imports if absent:

```python
import json
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py::test_create_run_persists_expected_boot_failure tests/mcp/lifecycle/test_runs_tools.py::test_create_run_rejects_bad_expected_boot_failure tests/mcp/lifecycle/test_runs_tools.py::test_get_run_exposes_expected_boot_failure -q
```

Expected: failures because the tool signature and envelope do not yet support the field.

- [ ] **Step 3: Validate expected boot failure in `runs.create`**

In `src/kdive/mcp/tools/lifecycle/runs/create.py`, add imports:

```python
from pydantic import ValidationError
from kdive.domain.models import ExpectedBootFailure
```

Add helper:

```python
def _parse_expected_boot_failure(
    object_id: str, value: dict[str, Any] | None
) -> dict[str, Any] | ToolResponse | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return _config_error(object_id, data={"reason": "bad_expected_boot_failure"})
    try:
        parsed = ExpectedBootFailure.model_validate(value)
    except ValidationError:
        return _config_error(object_id, data={"reason": "bad_expected_boot_failure"})
    return parsed.model_dump(mode="json", exclude_none=True)
```

Change `create_run` signature:

```python
async def create_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    investigation_id: str,
    system_id: str,
    build_profile: dict[str, Any],
    expected_boot_failure: dict[str, Any] | None = None,
) -> ToolResponse:
```

After build-profile parsing, add:

```python
    parsed_expected = _parse_expected_boot_failure(system_id, expected_boot_failure)
    if isinstance(parsed_expected, ToolResponse):
        return parsed_expected
```

Pass `parsed_expected` into `_create_locked`.

Change `_create_locked` signature:

```python
async def _create_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    inv_uid: UUID,
    sys_uid: UUID,
    build_profile: ParsedBuildProfile,
    expected_boot_failure: dict[str, Any] | None,
    *,
    project: str,
) -> ToolResponse:
```

Add the field to `Run(...)`:

```python
expected_boot_failure=expected_boot_failure,
```

Add success data:

```python
        data={
            "project": project,
            "investigation_id": str(inv_uid),
            "system_id": str(sys_uid),
            **(
                {"expected_boot_failure": expected_boot_failure["kind"]}
                if expected_boot_failure is not None
                else {}
            ),
        },
```

- [ ] **Step 4: Expose expected boot failure in `runs.get`**

In `src/kdive/mcp/tools/lifecycle/runs/common.py`, add import:

```python
import json
```

Update `envelope_for_run` data construction:

```python
    data = {"project": run.project}
    if required_cmdline is not None:
        data["required_cmdline"] = required_cmdline
    if run.expected_boot_failure is not None:
        kind = run.expected_boot_failure.get("kind")
        if isinstance(kind, str):
            data["expected_boot_failure"] = kind
        data["expected_boot_failure_json"] = json.dumps(
            run.expected_boot_failure, separators=(",", ":"), sort_keys=True
        )
```

- [ ] **Step 5: Update FastMCP registration signature**

Find the `runs.create` registration function and add the optional parameter to the tool wrapper.
Use this shape:

```python
expected_boot_failure: Annotated[
    dict[str, Any] | None,
    Field(description="Optional expected boot failure, e.g. {'kind':'console_crash','pattern':'Oops|__d_lookup'}."),
] = None,
```

Pass it through:

```python
expected_boot_failure=expected_boot_failure,
```

- [ ] **Step 6: Run tests to verify they pass**

Run:

```bash
uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py::test_create_run_persists_expected_boot_failure tests/mcp/lifecycle/test_runs_tools.py::test_create_run_rejects_bad_expected_boot_failure tests/mcp/lifecycle/test_runs_tools.py::test_get_run_exposes_expected_boot_failure -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/runs/create.py src/kdive/mcp/tools/lifecycle/runs/common.py tests/mcp/lifecycle/test_runs_tools.py
git commit -m "feat: expose expected boot failure on runs"
```

---

### Task 3: Bounded Literal Artifact Search Helper

**Files:**
- Create: `src/kdive/security/artifact_search.py`
- Test: `tests/security/test_artifact_search.py`

- [ ] **Step 1: Write failing helper tests**

Create `tests/security/test_artifact_search.py`:

```python
import pytest

from kdive.security.artifact_search import (
    ArtifactSearchInputError,
    parse_literal_terms,
    search_text,
)


def test_parse_literal_terms_splits_or_terms() -> None:
    assert parse_literal_terms("__d_lookup|Oops") == ("__d_lookup", "Oops")


@pytest.mark.parametrize("pattern", ["", "a||b", "bad\x00term"])
def test_parse_literal_terms_rejects_bad_patterns(pattern: str) -> None:
    with pytest.raises(ArtifactSearchInputError):
        parse_literal_terms(pattern)


def test_parse_literal_terms_rejects_too_many_terms() -> None:
    with pytest.raises(ArtifactSearchInputError):
        parse_literal_terms("|".join(f"t{i}" for i in range(17)))


def test_search_text_returns_bounded_context() -> None:
    data = b"line one\npanic start\nRIP: __d_lookup+0x1\nnext line\n"
    result = search_text(
        data,
        pattern="__d_lookup|Oops",
        before_lines=1,
        after_lines=1,
        max_matches=5,
    )
    assert result.match_count == 1
    assert result.truncated is False
    assert result.matches[0]["line"] == 3
    assert result.matches[0]["before"] == ["panic start"]
    assert result.matches[0]["after"] == ["next line"]


def test_search_text_clips_long_lines_and_total_json() -> None:
    data = ("x" * 900 + " NEEDLE\n").encode()
    result = search_text(data, pattern="NEEDLE", before_lines=0, after_lines=0, max_matches=1)
    assert len(result.matches[0]["text"]) <= 512 + len("...[clipped]")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run python -m pytest tests/security/test_artifact_search.py -q
```

Expected: import failure because `kdive.security.artifact_search` does not exist.

- [ ] **Step 3: Implement helper**

Create `src/kdive/security/artifact_search.py`:

```python
"""Bounded literal text search over redacted artifacts (ADR-0064)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_PATTERN_CHARS = 256
MAX_TERMS = 16
MAX_LINE_CHARS = 512
MAX_MATCHES_JSON_CHARS = 64 * 1024
CLIPPED = "...[clipped]"


class ArtifactSearchInputError(ValueError):
    """The requested search is malformed or outside the ADR-0064 bounds."""


@dataclass(frozen=True)
class SearchResult:
    """A bounded search result suitable for `ToolResponse.data`."""

    matches: list[dict[str, Any]]
    match_count: int
    truncated: bool

    def matches_json(self) -> str:
        return json.dumps(self.matches, ensure_ascii=False, separators=(",", ":"))


def parse_literal_terms(pattern: str) -> tuple[str, ...]:
    """Parse a grep-style literal OR pattern (`term1|term2`) into bounded terms."""
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_CHARS:
        raise ArtifactSearchInputError("pattern must be 1-256 characters")
    if "\x00" in pattern:
        raise ArtifactSearchInputError("pattern must not contain NUL")
    terms = tuple(part for part in pattern.split("|"))
    if not terms or any(term == "" for term in terms):
        raise ArtifactSearchInputError("pattern contains an empty term")
    if len(terms) > MAX_TERMS:
        raise ArtifactSearchInputError("pattern has too many terms")
    return terms


def _clip(text: str) -> str:
    if len(text) <= MAX_LINE_CHARS:
        return text
    return text[:MAX_LINE_CHARS] + CLIPPED


def _bounded_int(value: int, *, low: int, high: int, label: str) -> int:
    if value < low or value > high:
        raise ArtifactSearchInputError(f"{label} out of range")
    return value


def search_text(
    data: bytes,
    *,
    pattern: str,
    before_lines: int = 2,
    after_lines: int = 4,
    max_matches: int = 20,
) -> SearchResult:
    """Search UTF-8-ish bytes line-by-line with bounded context windows."""
    terms = parse_literal_terms(pattern)
    before_lines = _bounded_int(before_lines, low=0, high=10, label="before_lines")
    after_lines = _bounded_int(after_lines, low=0, high=20, label="after_lines")
    max_matches = _bounded_int(max_matches, low=1, high=50, label="max_matches")
    lines = data.decode("utf-8", errors="replace").splitlines()
    matches: list[dict[str, Any]] = []
    truncated = False
    for idx, line in enumerate(lines):
        if not any(term in line for term in terms):
            continue
        start = max(0, idx - before_lines)
        end = min(len(lines), idx + after_lines + 1)
        candidate = {
            "line": idx + 1,
            "text": _clip(line),
            "before": [_clip(item) for item in lines[start:idx]],
            "after": [_clip(item) for item in lines[idx + 1 : end]],
        }
        trial = [*matches, candidate]
        encoded = json.dumps(trial, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > MAX_MATCHES_JSON_CHARS:
            truncated = True
            break
        matches.append(candidate)
        if len(matches) >= max_matches:
            truncated = True
            break
    return SearchResult(matches=matches, match_count=len(matches), truncated=truncated)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run python -m pytest tests/security/test_artifact_search.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/security/artifact_search.py tests/security/test_artifact_search.py
git commit -m "feat: add bounded artifact text search"
```

---

### Task 4: `artifacts.search_text` MCP Tool

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/artifacts.py`
- Test: `tests/mcp/catalog/test_artifacts_tools.py`
- Modify: `docs/guide/reference/artifacts.md`
- Modify: `docs/guide/reference/index.md`

- [ ] **Step 1: Write failing artifact-search tests**

In `tests/mcp/catalog/test_artifacts_tools.py`, add a fake store:

```python
from kdive.domain.models import Sensitivity
from kdive.store.objectstore import FetchedArtifact, HeadResult


class _SearchStore:
    def __init__(self, data: bytes, *, size: int | None = None) -> None:
        self.data = data
        self.size = len(data) if size is None else size
        self.headed = False
        self.got = False

    def head(self, key: str) -> HeadResult | None:
        self.headed = True
        return HeadResult(size_bytes=self.size, checksum_sha256=None, etag="e")

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        self.got = True
        assert etag == "e"
        return FetchedArtifact(self.data, Sensitivity.REDACTED, "console")
```

Add tests:

```python
def test_artifacts_search_text_returns_bounded_matches(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"before\nRIP: __d_lookup+0x1\nafter\n")
            resp = await artifacts_tools.artifacts_search_text(
                pool,
                _ctx(),
                artifact_id=red_id,
                pattern="__d_lookup|Oops",
                before_lines=1,
                after_lines=1,
                store=store,
            )
        assert resp.status == "searched"
        assert resp.data["match_count"] == "1"
        matches = json.loads(resp.data["matches_json"])
        assert matches[0]["line"] == 2
        assert matches[0]["before"] == ["before"]
        assert matches[0]["after"] == ["after"]

    asyncio.run(_run())


def test_artifacts_search_text_sensitive_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, sens_id, _ = await _seed_system_with_artifacts(pool)
            resp = await artifacts_tools.artifacts_search_text(
                pool, _ctx(), artifact_id=sens_id, pattern="panic", store=_SearchStore(b"panic")
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_artifacts_search_text_requires_viewer(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            with pytest.raises(AuthorizationError):
                await artifacts_tools.artifacts_search_text(
                    pool, _ctx(role=None), artifact_id=red_id, pattern="panic"
                )

    asyncio.run(_run())


def test_artifacts_search_text_rejects_oversized_before_get(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"", size=1024 * 1024 + 1)
            resp = await artifacts_tools.artifacts_search_text(
                pool, _ctx(), artifact_id=red_id, pattern="panic", store=store
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "artifact_too_large"
        assert store.got is False

    asyncio.run(_run())


def test_artifacts_search_text_rejects_bad_pattern_before_head(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            _, _, red_id = await _seed_system_with_artifacts(pool)
            store = _SearchStore(b"panic")
            resp = await artifacts_tools.artifacts_search_text(
                pool, _ctx(), artifact_id=red_id, pattern="a||b", store=store
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "bad_search_input"
        assert store.headed is False
        assert store.got is False

    asyncio.run(_run())
```

Add imports if absent:

```python
import json
from typing import Protocol
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run python -m pytest tests/mcp/catalog/test_artifacts_tools.py::test_artifacts_search_text_returns_bounded_matches tests/mcp/catalog/test_artifacts_tools.py::test_artifacts_search_text_sensitive_is_not_found tests/mcp/catalog/test_artifacts_tools.py::test_artifacts_search_text_requires_viewer tests/mcp/catalog/test_artifacts_tools.py::test_artifacts_search_text_rejects_oversized_before_get tests/mcp/catalog/test_artifacts_tools.py::test_artifacts_search_text_rejects_bad_pattern_before_head -q
```

Expected: failures because `artifacts_search_text` does not exist.

- [ ] **Step 3: Implement tool handler**

In `src/kdive/mcp/tools/catalog/artifacts.py`, add imports:

```python
import asyncio
from kdive.security.artifact_search import (
    ArtifactSearchInputError,
    parse_literal_terms,
    search_text,
)
from kdive.store.objectstore import FetchedArtifact, HeadResult
```

Add protocol:

```python
class _SearchStore(Protocol):
    def head(self, key: str) -> HeadResult | None: ...
    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...
```

Add constants:

```python
_MAX_SEARCHABLE_ARTIFACT_BYTES = 1024 * 1024
```

Add function:

```python
async def artifacts_search_text(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    artifact_id: str,
    pattern: str,
    before_lines: int = 2,
    after_lines: int = 4,
    max_matches: int = 20,
    store: _SearchStore | None = None,
) -> ToolResponse:
    """Search one redacted System-owned text artifact with bounded literal context."""
    uid = _as_uuid(artifact_id)
    if uid is None:
        return _config_error(artifact_id)
    try:
        parse_literal_terms(pattern)
    except ArtifactSearchInputError:
        return _config_error(artifact_id, data={"reason": "bad_search_input"})
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_GET_SQL, (uid,))
            row = await cur.fetchone()
            if row is None:
                return _config_error(artifact_id)
            await cur.execute(_PROJECT_SQL, (row["owner_id"],))
            owner = await cur.fetchone()
        if owner is None or owner["project"] not in ctx.projects:
            return _config_error(artifact_id)
        require_role(ctx, owner["project"], Role.VIEWER)
    store = store or object_store_from_env()
    key = str(row["object_key"])
    try:
        head = await asyncio.to_thread(store.head, key)
    except CategorizedError as exc:
        return ToolResponse.failure(artifact_id, exc.category)
    if head is None:
        return _config_error(artifact_id)
    if head.size_bytes > _MAX_SEARCHABLE_ARTIFACT_BYTES:
        return _config_error(
            artifact_id,
            data={"reason": "artifact_too_large", "size_bytes": str(head.size_bytes)},
        )
    try:
        fetched = await asyncio.to_thread(store.get_artifact, key, head.etag)
        if fetched.sensitivity is not Sensitivity.REDACTED:
            return _config_error(artifact_id)
        result = search_text(
            fetched.data,
            pattern=pattern,
            before_lines=before_lines,
            after_lines=after_lines,
            max_matches=max_matches,
        )
    except ArtifactSearchInputError:
        return _config_error(artifact_id, data={"reason": "bad_search_input"})
    except CategorizedError as exc:
        return ToolResponse.failure(artifact_id, exc.category)
    return ToolResponse.success(
        artifact_id,
        "searched",
        suggested_next_actions=["artifacts.search_text", "runs.get"],
        refs={"artifact": key},
        data={
            "match_count": str(result.match_count),
            "truncated": str(result.truncated).lower(),
            "matches_json": result.matches_json(),
        },
    )
```

- [ ] **Step 4: Register FastMCP tool**

In `register(...)`, add:

```python
    @app.tool(
        name="artifacts.search_text",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def artifacts_search_text_tool(
        artifact_id: Annotated[str, Field(description="The redacted System artifact id.")],
        pattern: Annotated[
            str,
            Field(description="Literal OR search pattern, e.g. '__d_lookup|Oops|panic'."),
        ],
        before_lines: Annotated[int, Field(description="Context lines before each match.")] = 2,
        after_lines: Annotated[int, Field(description="Context lines after each match.")] = 4,
        max_matches: Annotated[int, Field(description="Maximum match windows to return.")] = 20,
    ) -> ToolResponse:
        """Search a redacted System artifact with bounded literal line context."""
        return await artifacts_search_text(
            pool,
            current_context(),
            artifact_id=artifact_id,
            pattern=pattern,
            before_lines=before_lines,
            after_lines=after_lines,
            max_matches=max_matches,
        )
```

- [ ] **Step 5: Update docs**

Add to `docs/guide/reference/artifacts.md`:

```markdown
## `artifacts.search_text`

Search one redacted System-owned text artifact. Requires viewer; sensitive ids are not-found.

| param | type | required | description |
|---|---|---|---|
| `artifact_id` | `string` | yes | The redacted System artifact to search. |
| `pattern` | `string` | yes | Literal OR pattern, such as `__d_lookup|Oops|panic`. |
| `before_lines` | `integer` | no | Context lines before each match. |
| `after_lines` | `integer` | no | Context lines after each match. |
| `max_matches` | `integer` | no | Maximum match windows to return. |
```

Add one row to `docs/guide/reference/index.md`:

```markdown
| [`artifacts.search_text`](artifacts.md#artifactssearch_text) | `partial` |
```

- [ ] **Step 6: Run tests to verify they pass**

Run:

```bash
uv run python -m pytest tests/security/test_artifact_search.py tests/mcp/catalog/test_artifacts_tools.py::test_artifacts_search_text_returns_bounded_matches tests/mcp/catalog/test_artifacts_tools.py::test_artifacts_search_text_sensitive_is_not_found tests/mcp/catalog/test_artifacts_tools.py::test_artifacts_search_text_requires_viewer tests/mcp/catalog/test_artifacts_tools.py::test_artifacts_search_text_rejects_oversized_before_get tests/mcp/catalog/test_artifacts_tools.py::test_artifacts_search_text_rejects_bad_pattern_before_head -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/mcp/tools/catalog/artifacts.py src/kdive/security/artifact_search.py tests/mcp/catalog/test_artifacts_tools.py tests/security/test_artifact_search.py docs/guide/reference/artifacts.md docs/guide/reference/index.md
git commit -m "feat: add redacted artifact text search"
```

---

### Task 5: Expected-Crash Boot Outcome

**Files:**
- Modify: `src/kdive/jobs/handlers/runs.py`
- Test: `tests/mcp/lifecycle/test_runs_tools.py`
- Test: `tests/mcp/debug/test_debug_tools.py`

- [ ] **Step 1: Write failing boot-handler tests**

Add helper to `tests/mcp/lifecycle/test_runs_tools.py`:

```python
async def _set_expected_boot_failure(
    pool: AsyncConnectionPool, run_id: str, pattern: str = "__d_lookup|Oops"
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET expected_boot_failure=%s WHERE id=%s",
            (Jsonb({"kind": "console_crash", "pattern": pattern}), run_id),
        )
```

Add tests near the current boot handler tests:

```python
def test_boot_handler_records_expected_crash_observed(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runs_handlers, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_handlers, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _set_expected_boot_failure(pool, run_id)
            await _record_install_step(pool, run_id)
            sid = await _system_id_of(pool, run_id)
            (tmp_path / f"{sid}.log").write_bytes(b"Kernel panic\nRIP: __d_lookup+0x1\n")
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(error=ErrorCategory.READINESS_FAILURE)
            async with pool.connection() as conn:
                result = await runs_handlers.boot_handler(conn, job, booter)
            assert result == run_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, result FROM run_steps WHERE run_id=%s AND step='boot'",
                    (run_id,),
                )
                step = await cur.fetchone()
                await cur.execute("SELECT state FROM systems WHERE id=%s", (sid,))
                system = await cur.fetchone()
        assert step is not None
        assert step["state"] == "succeeded"
        assert step["result"]["boot_outcome"] == "expected_crash_observed"
        assert step["result"]["expectation_matched"] is True
        assert step["result"]["evidence_kind"] == "console"
        assert step["result"]["evidence_artifact_id"]
        assert system is not None
        assert system["state"] == "ready"

    asyncio.run(_run())


def test_boot_handler_expected_crash_requires_matching_console(
    migrated_url: str,
    minio_store: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runs_handlers, "object_store_from_env", lambda: minio_store)
    monkeypatch.setattr(runs_handlers, "console_log_path", lambda sid: tmp_path / f"{sid}.log")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_succeeded_run(pool)
            await _set_expected_boot_failure(pool, run_id, pattern="__d_lookup")
            await _record_install_step(pool, run_id)
            sid = await _system_id_of(pool, run_id)
            (tmp_path / f"{sid}.log").write_bytes(b"Kernel panic\nRIP: other_symbol\n")
            job = await _enqueue_job(pool, JobKind.BOOT, run_id, "boot")
            booter = _FakeBooter(error=ErrorCategory.READINESS_FAILURE)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await runs_handlers.boot_handler(conn, job, booter)
            nsteps = await _count(
                pool,
                "SELECT count(*) AS n FROM run_steps WHERE run_id=%s AND step='boot'",
                (run_id,),
            )
        assert caught.value.category is ErrorCategory.READINESS_FAILURE
        assert nsteps == 0

    asyncio.run(_run())
```

Add a debug-session regression test to `tests/mcp/debug/test_debug_tools.py` near
`test_start_session_non_ready_system_is_config_error`:

```python
def test_start_session_rejects_expected_crash_run(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(
                pool,
                sys_id,
                boot_result={"boot_outcome": "expected_crash_observed"},
            )
            conn_fake = _FakeConnector()
            resp = await debug_tools.start_session(
                pool,
                _ctx(),
                run_id=run_id,
                transport="gdbstub",
                connector=conn_fake,
            )
            count = await _session_count(pool)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "expected_crash_not_live_debuggable"
        assert count == 0
        assert conn_fake.opened == []

    asyncio.run(_run())
```

The test must prove the expected-crash Run outcome does not become attachable through the
existing live-session path while preserving the System for the next Run.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py::test_boot_handler_records_expected_crash_observed tests/mcp/lifecycle/test_runs_tools.py::test_boot_handler_expected_crash_requires_matching_console -q
```

Expected: first test fails because `boot_handler` re-raises `READINESS_FAILURE`; the
debug-session regression fails until attach preconditions inspect the boot ledger.
Run the debug regression separately while it is still red:

```bash
uv run python -m pytest tests/mcp/debug/test_debug_tools.py::test_start_session_rejects_expected_crash_run -q
```

- [ ] **Step 3: Refactor console capture into a helper**

In `src/kdive/jobs/handlers/runs.py`, add imports:

```python
from kdive.security.artifact_search import ArtifactSearchInputError, search_text
```

Also import `SystemState` alongside `RunState`.

Add a named tuple near `_ConsoleRow`:

```python
class _ConsoleArtifact(NamedTuple):
    id: UUID
    object_key: str
    data: bytes
```

Extract the current `finally` console registration logic into:

```python
async def _capture_console_artifact(
    conn: AsyncConnection, system_id: UUID
) -> _ConsoleArtifact | None:
    try:
        raw = await asyncio.to_thread(read_console_log, console_log_path(system_id))
        if not raw:
            _log.warning(
                "console log for system %s is empty or unreadable; registering no console artifact",
                system_id,
            )
            return None
        redacted = Redactor().redact_text(raw.decode("utf-8", "replace")).encode("utf-8")
        stored = await asyncio.to_thread(
            lambda: object_store_from_env().put_artifact(
                ArtifactWriteRequest(
                    tenant="local",
                    owner_kind="systems",
                    owner_id=str(system_id),
                    name="console",
                    data=redacted,
                    sensitivity=Sensitivity.REDACTED,
                    retention_class="console",
                )
            )
        )
        async with conn.transaction():
            existing = await _existing_console_row(conn, system_id)
            if existing is None:
                inserted = await ARTIFACTS.insert(
                    conn, register_artifact_row(stored, owner_kind="systems", owner_id=system_id)
                )
                return _ConsoleArtifact(inserted.id, inserted.object_key, redacted)
            if existing.etag != stored.etag:
                await conn.execute(_REFRESH_CONSOLE_ETAG_SQL, (stored.etag, existing.id))
            return _ConsoleArtifact(existing.id, stored.key, redacted)
    except Exception:
        _log.warning(
            "console artifact registration failed for system %s; boot outcome unaffected",
            system_id,
            exc_info=True,
        )
        return None
```

Replace the `finally` block in `boot_handler` with calls to this helper after Task 5 step 4.

- [ ] **Step 4: Add expected-crash evaluator**

Add helper:

```python
def _expected_crash_matches(run: Run, redacted_console: bytes) -> bool:
    expected = run.expected_boot_failure
    if expected is None or expected.get("kind") != "console_crash":
        return False
    pattern = expected.get("pattern")
    if not isinstance(pattern, str):
        return False
    try:
        return search_text(
            redacted_console,
            pattern=pattern,
            before_lines=0,
            after_lines=0,
            max_matches=1,
        ).match_count > 0
    except ArtifactSearchInputError:
        return False
```

Do not mark the System crashed when expected-crash evidence matches. The expected-crash result is
Run-scoped evidence; the System remains reusable for the next vulnerable/fixed A/B Run.

Add a boot-specific step wrapper that preserves the lock order while keeping the provider call
inside the idempotency check:

```python
async def _boot_step_locked(
    conn: AsyncConnection,
    system_id: UUID,
    run_id: UUID,
    fn: Callable[[], Awaitable[dict[str, Any]]],
) -> None:
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, system_id),
        advisory_xact_lock(conn, LockScope.RUN, run_id),
    ):
        await run_step(conn, run_id, "boot", fn)
```

If using this helper means reading the console twice, keep the duplicate read for the first
implementation. The console file is local and bounded by the existing capture path. Avoid adding
object-store search into the worker; the worker already has the redacted bytes before storage.

- [ ] **Step 5: Update `boot_handler` control flow**

Replace `boot_handler` with this shape. The provider call stays inside `_boot_step_locked`, so a
replayed boot job returns the existing ledger result without calling `booter.boot` again.

```python
async def boot_handler(conn: AsyncConnection, job: Job, booter: Booter) -> str | None:
    """Boot the installed kernel and confirm run-readiness, recording the `boot` step."""
    run_id = UUID(load_payload(job, RunPayload).run_id)
    run = await RUNS.get(conn, run_id)
    if run is None:
        raise CategorizedError(
            "boot target run is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"run_id": str(run_id)},
    )
    job_ctx = job_context_from_job(job, run.project)

    async def _record_boot_audit() -> None:
        await audit.record(
            conn,
            job_ctx,
            audit.AuditEvent(
                tool="runs.boot",
                object_kind="runs",
                object_id=run_id,
                transition="boot",
                args={"run_id": str(run_id)},
                project=run.project,
            ),
        )

    async def _do() -> dict[str, Any]:
        try:
            await asyncio.to_thread(booter.boot, run.system_id)
        except CategorizedError as exc:
            artifact = await _capture_console_artifact(conn, run.system_id)
            if (
                exc.category is ErrorCategory.READINESS_FAILURE
                and artifact is not None
                and artifact.data
                and _expected_crash_matches(run, artifact.data)
            ):
                await _record_boot_audit()
                return {
                    "system_id": str(run.system_id),
                    "boot_outcome": "expected_crash_observed",
                    "expectation_matched": True,
                    "evidence_kind": "console",
                    "evidence_artifact_id": str(artifact.id),
                }
            raise
        artifact = await _capture_console_artifact(conn, run.system_id)
        await _record_boot_audit()
        return {
            "system_id": str(run.system_id),
            "boot_outcome": "ready",
            **({"evidence_artifact_id": str(artifact.id)} if artifact else {}),
        }

    await _boot_step_locked(conn, run.system_id, run_id, _do)
    return str(run_id)
```

Keep `_capture_console_artifact` inside `_do`, not in a `finally` outside `_boot_step_locked`.
That preserves the current "do not call `booter.boot` on replay" idempotency behavior.

Do not use this non-idempotent shape:

```python
try:
    await asyncio.to_thread(booter.boot, run.system_id)
finally:
    ...
await _run_step_locked(conn, run_id, "boot", _do)
```

The provider call would run before the idempotency check.

- [ ] **Step 6: Run targeted boot tests**

Run:

```bash
uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py::test_boot_handler_idempotent_on_existing_step tests/mcp/lifecycle/test_runs_tools.py::test_boot_handler_failure_records_no_step tests/mcp/lifecycle/test_runs_tools.py::test_boot_handler_registers_console_even_on_failure tests/mcp/lifecycle/test_runs_tools.py::test_boot_handler_records_expected_crash_observed tests/mcp/lifecycle/test_runs_tools.py::test_boot_handler_expected_crash_requires_matching_console -q
```

Expected: all pass.

Also run:

```bash
uv run python -m pytest tests/mcp/debug/test_debug_tools.py::test_start_session_rejects_expected_crash_run -q
```

Expected: pass, proving the expected-crash reproduction result is not an implicit live-debug
attach mechanism.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/jobs/handlers/runs.py src/kdive/mcp/tools/debug/sessions.py tests/mcp/lifecycle/test_runs_tools.py tests/mcp/debug/test_debug_tools.py
git commit -m "feat: record expected boot crash outcomes"
```

---

### Task 6: Integration Verification And Docs Gates

**Files:**
- No source edits are planned in this task. Stop and investigate if any gate fails.

- [ ] **Step 1: Run focused MCP tests**

Run:

```bash
uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py tests/mcp/catalog/test_artifacts_tools.py tests/mcp/debug/test_debug_tools.py::test_start_session_rejects_expected_crash_run tests/security/test_artifact_search.py -q
```

Expected: all pass.

- [ ] **Step 2: Run migration and model tests**

Run:

```bash
uv run python -m pytest tests/db/test_migrate.py tests/domain/test_models.py -q
```

Expected: all pass.

- [ ] **Step 3: Run tool docs tests**

Run:

```bash
uv run python -m pytest tests/mcp/core/test_tool_docs.py -q
```

Expected: all pass.

- [ ] **Step 4: Run lint/type gates**

Run:

```bash
just lint
just type
```

Expected: both clean.

- [ ] **Step 5: Run the full non-live suite**

Run:

```bash
just test
```

Expected: full suite passes with the existing live markers skipped/deselected.

---

## Self-Review Checklist

- ADR-0064 requirement: Run-scoped expected boot failure metadata.
  - Covered by Tasks 1 and 2.
- ADR-0064 requirement: unexpected crashes remain failures.
  - Covered by Task 5 tests preserving `test_boot_handler_failure_records_no_step`.
- ADR-0064 requirement: expected crash can be a successful reproduction outcome.
  - Covered by Task 5 expected-crash test and boot handler changes.
- ADR-0064 requirement: no full logs in ordinary envelopes.
  - Covered by Task 4 returning bounded `matches_json` only.
- ADR-0064 requirement: redacted System-owned artifact search only.
  - Covered by Task 4 SQL access via `_GET_SQL` and sensitive/cross-project tests.
- ADR-0064 requirement: size-gate before fetch.
  - Covered by Task 4 oversized test asserting `get_artifact` is not called.
- Response-envelope invariant: `ToolResponse.data` remains `dict[str, str]`.
  - Covered by JSON-in-string fields in Tasks 2 and 4.
- No dcache-specific provider behavior.
  - Covered by generic pattern metadata and literal search.

---

## Execution Handoff

Plan complete and saved to
`docs/superpowers/plans/2026-06-07-expected-boot-failures-artifact-search.md`.

Execute task-by-task. Prefer subagent-driven execution for independent tasks, with review after
each task, unless the user asks for inline execution in the current session.
