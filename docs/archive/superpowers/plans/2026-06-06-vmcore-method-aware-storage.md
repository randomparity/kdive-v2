# Method-aware vmcore storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make vmcore capture storage method-aware so a second `vmcore.fetch` with a different method fails with `configuration_error` instead of silently returning the first method's core (closes #118).

**Architecture:** Encode the capturing method in the raw object key (`vmcore-{method}`); resolve the single raw core in all three readers via `LIKE '%/vmcore-%'` excluding the `-redacted` derivative; enforce first-method-wins in the `capture_vmcore` handler (precheck = orphan-free common case, finalize = race backstop) under the existing per-System advisory lock. No new agent-facing tool argument.

**Tech Stack:** Python 3.13, psycopg (async), pytest. Guardrails: `just lint`, `just type`, `just test`.

**Spec:** `docs/superpowers/specs/2026-06-06-vmcore-method-aware-storage-design.md` · **ADR:** `docs/adr/0050-vmcore-method-aware-storage.md`

---

## File structure

- `src/kdive/providers/local_libvirt/retrieve.py` — producer: name objects `vmcore-{method}` / `vmcore-{method}-redacted`.
- `src/kdive/mcp/tools/vmcore.py` — raw-key SQL, `_existing_raw_key`, `list_vmcores` filter, `_captured_method` + conflict guard, `_precheck_system`/`_finalize_capture`/`capture_handler` threading the method.
- `src/kdive/mcp/tools/introspect.py` — raw-key SQL (reader).
- Tests: `tests/mcp/test_vmcore_tools.py`, `tests/mcp/test_introspect_tools.py`, `tests/integration/test_walking_skeleton.py`.

Two commits: **Task 1** is a behavior-preserving rename (producer + readers + fixtures move to the suffixed key in lockstep — existing tests stay green). **Task 2** adds the method-aware reject + new tests. This split bisects cleanly: Task 1 changes the key format, Task 2 changes the dedup behavior.

---

## Task 1: Rename the raw core key to `vmcore-{method}` (readers + fixtures in lockstep)

This task changes the object key format only. Existing behavior (return existing raw core on
re-dispatch, single core per System) is preserved; tests move to the new key shape and stay green.

**Files:**
- Modify: `src/kdive/providers/local_libvirt/retrieve.py:180-184`
- Modify: `src/kdive/mcp/tools/vmcore.py:88-91` (`_RAW_KEY_SQL`), `:181-186` (`_existing_raw_key`), `:248-253` (`list_vmcores`), `:295-300` (`_resolve_postmortem` cursor)
- Modify: `src/kdive/mcp/tools/introspect.py:46-48` (`_RAW_KEY_SQL`), `:96-97` (cursor)
- Test: `tests/mcp/test_vmcore_tools.py`, `tests/mcp/test_introspect_tools.py`, `tests/providers/local_libvirt/test_retrieve.py`, `tests/integration/test_walking_skeleton.py`, `tests/integration/test_live_stack.py`

> **Exhaustiveness (verified by `rg -n '/vmcore"' tests/ src/`):** the raw-key literal `…/vmcore`
> appears in `retrieve.py` (producer), `vmcore.py`/`introspect.py` (readers, this task), and these
> test files: `test_vmcore_tools.py` (×4: `_capture_output`, ref asserts, idempotency insert,
> redaction guard), `test_introspect_tools.py` (×2: seed insert, `endswith` assert),
> `test_retrieve.py` (×5: producer-unit asserts + `_FakeStore(fail_on=...)`),
> `test_walking_skeleton.py` (×2: fake keys, line-295 guard), `test_live_stack.py` (×1: line-525
> guard, gated). `test_artifacts_tools.py` also inserts `vmcore`/`vmcore-redacted` rows but asserts
> on **sensitivity**, not key shape — unaffected. All of these are edited below; re-run the `rg`
> sweep before committing to confirm nothing new crept in.

- [ ] **Step 1: Update the producer to method-suffixed keys**

In `retrieve.py`, in `capture()` (currently lines 180-184), replace the `_put` names:

```python
        build_id = self._read_vmcore_build_id(data)
        raw = self._put(system_id, f"vmcore-{method.value}", data, Sensitivity.SENSITIVE)
        redacted = self._put(
            system_id,
            f"vmcore-{method.value}-redacted",
            self._extract_redacted(data),
            Sensitivity.REDACTED,
        )
        return CaptureOutput(raw=raw, redacted=redacted, vmcore_build_id=build_id)
```

- [ ] **Step 2: Update the raw-key SQL + `_existing_raw_key` in `vmcore.py`**

Replace the `_RAW_KEY_SQL` constant (lines 88-91) and add the two LIKE-pattern constants:

```python
_RAW_KEY_SQL: LiteralString = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s "
    "AND object_key LIKE %s AND object_key NOT LIKE %s"
)
_RAW_KEY_LIKE = "%/vmcore-%"
_REDACTED_LIKE = "%-redacted"
```

Update `_existing_raw_key` (lines 181-186) to pass both patterns:

```python
async def _existing_raw_key(conn: AsyncConnection, system_id: UUID) -> str | None:
    """Return the System's raw `vmcore-{method}` object key, or ``None`` (the execution ledger)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RAW_KEY_SQL, (system_id, _RAW_KEY_LIKE, _REDACTED_LIKE))
        row = await cur.fetchone()
    return None if row is None else str(row["object_key"])
```

Update the `_resolve_postmortem` cursor (lines 295-297) to pass both patterns:

```python
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RAW_KEY_SQL, (run.system_id, _RAW_KEY_LIKE, _REDACTED_LIKE))
        row = await cur.fetchone()
```

- [ ] **Step 3: Update the `list_vmcores` redacted filter in `vmcore.py`**

Add a helper above `list_vmcores` and use it (replacing the `endswith("/vmcore-redacted")` check at line 253):

```python
def _is_redacted_vmcore(object_key: str) -> bool:
    """True for a redacted vmcore derivative key (`.../vmcore-{method}-redacted`)."""
    return "/vmcore-" in object_key and object_key.endswith("-redacted")
```

```python
    listed = await artifacts_tools.artifacts_list(pool, ctx, system_id=system_id)
    return [r for r in listed if _is_redacted_vmcore(r.refs.get("object", ""))]
```

- [ ] **Step 4: Update the raw-key SQL in `introspect.py`**

Replace `_RAW_KEY_SQL` (lines 46-48) to mirror `vmcore.py`, and update the cursor at line 96-97:

```python
_RAW_KEY_SQL: LiteralString = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s "
    "AND object_key LIKE %s AND object_key NOT LIKE %s"
)
_RAW_KEY_LIKE = "%/vmcore-%"
_REDACTED_LIKE = "%-redacted"
```

```python
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RAW_KEY_SQL, (run.system_id, _RAW_KEY_LIKE, _REDACTED_LIKE))
        row = await cur.fetchone()
```

- [ ] **Step 5: Move the test fixtures/fakes to the new key shape**

`tests/mcp/test_vmcore_tools.py` — make `_capture_output` method-aware (lines 46-51):

```python
def _capture_output(sys_id: str, method: CaptureMethod = CaptureMethod.HOST_DUMP) -> CaptureOutput:
    raw = StoredArtifact(
        f"local/systems/{sys_id}/vmcore-{method.value}", "e1", Sensitivity.SENSITIVE, "vmcore"
    )
    red = StoredArtifact(
        f"local/systems/{sys_id}/vmcore-{method.value}-redacted", "e2", Sensitivity.REDACTED, "vmcore"
    )
    return CaptureOutput(raw=raw, redacted=red, vmcore_build_id="deadbeef")
```

Make `_FakeRetriever.capture` build output from the requested method (line 63-68):

```python
    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        self.calls += 1
        self.methods.append(method)
        if self._raises is not None:
            raise self._raises
        return _capture_output(self._sys_id, method)
```

Update `test_capture_handler_stores_rows_and_returns_ref` ref assertion (line 243):

```python
            assert ref == f"local/systems/{sys_id}/vmcore-host_dump"
```

Update `test_capture_handler_idempotent_skips_recapture` insert + assertion (lines 276-284):

```python
                await conn.execute(
                    "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                    "retention_class) VALUES ('systems', %s, %s, 'e', 'sensitive', 'vmcore')",
                    (sys_id, f"local/systems/{sys_id}/vmcore-host_dump"),
                )
            job = await _enqueue_capture(pool, sys_id)
            async with pool.connection() as conn:
                ref = await vmcore_tools.capture_handler(conn, job, _NoCaptureRetriever())
            assert ref == f"local/systems/{sys_id}/vmcore-host_dump"
```

Update `test_list_vmcores_redacted_only` assertion (line 331):

```python
        assert keys == {f"local/systems/{sys_id}/vmcore-host_dump-redacted"}
```

Update the surface-wide redaction guard `test_no_raw_vmcore_key_in_any_read_response` (line 471) to the non-vacuous form:

```python
        assert refs  # something was returned
        # A raw core is `.../vmcore-{method}` (no `-redacted`); it must never surface.
        assert all(not ("/vmcore-" in key and not key.endswith("-redacted")) for key in refs)
```

`tests/mcp/test_introspect_tools.py` — `_seed_vmcore_row` insert (line 76-77) and the `endswith` assertion (line 104):

```python
            (sys_id, f"local/systems/{sys_id}/vmcore-host_dump"),
```

```python
        assert str(port.kwargs["vmcore_ref"]).endswith("/vmcore-host_dump")
```

`tests/providers/local_libvirt/test_retrieve.py` — the producer's own unit test (this test
captures with `CaptureMethod.KDUMP`, so the suffix is `kdump`). Update
`test_capture_stores_two_artifacts_and_returns_build_id` (lines 92-99):

```python
    assert out.raw.key == f"{_TENANT}/systems/{_SYS}/vmcore-kdump"
    assert out.redacted.key == f"{_TENANT}/systems/{_SYS}/vmcore-kdump-redacted"
    assert out.vmcore_build_id == "deadbeef"
    names = {(name, sens) for _, name, sens, _ in store.puts}
    assert ("vmcore-kdump", Sensitivity.SENSITIVE) in names
    assert ("vmcore-kdump-redacted", Sensitivity.REDACTED) in names
    redacted_data = next(d for _, name, _, d in store.puts if name == "vmcore-kdump-redacted")
    assert b"hunter2" not in redacted_data and b"[REDACTED]" in redacted_data
```

And `test_capture_store_failure_is_infrastructure_failure` (line 109) — `_FakeStore.fail_on`
matches the object **name** exactly (`self.fail_on == name`), so it must name the suffixed object:

```python
        _retriever(_FakeStore(fail_on="vmcore-kdump"), core=b"X").capture(_SYS, CaptureMethod.KDUMP)
```

(`test_run_returns_redacted_crash_output` at line 130 passes `vmcore_ref="k/systems/s/vmcore"` as an
input ref to `run()`, not a stored key — `run()` fetches by the ref verbatim, so it needs no change.)

`tests/integration/test_walking_skeleton.py` — the fake capture keys (lines 86-94):

```python
        raw = StoredArtifact(
            f"local/systems/{self._system_id}/vmcore-host_dump", "e1", Sensitivity.SENSITIVE, "vmcore"
        )
        red = StoredArtifact(
            f"local/systems/{self._system_id}/vmcore-host_dump-redacted",
            "e2",
            Sensitivity.REDACTED,
            "vmcore",
        )
```

And the same vacuous-after-rename raw-key guard exists here at line 295 — replace it with the
non-vacuous form (identical to `test_vmcore_tools.py:471`):

```python
        assert all(not ("/vmcore-" in key and not key.endswith("-redacted")) for key in refs)
```

`tests/integration/test_live_stack.py` (gated `live_stack`, not run by `just test`, but the guard
must not silently go vacuous) — line 525:

```python
                assert all(
                    not ("/vmcore-" in r and not r.endswith("-redacted")) for r in refs
                ), "raw vmcore leaked (#1)"
```

- [ ] **Step 6: Sweep for missed sites, then run the FULL suite**

First confirm the edits are exhaustive (no stray raw-key literal remains un-updated):

Run: `rg -n '"%/vmcore"|/vmcore"' tests/ src/`
Expected: every hit is a string you intentionally changed to the `vmcore-{method}` shape or the
`%/vmcore-%`/`%-redacted` patterns; **no** bare `…/vmcore"` or `"%/vmcore"` literal survives in
`retrieve.py`, `vmcore.py`, `introspect.py`, `test_vmcore_tools.py`, `test_introspect_tools.py`,
`test_retrieve.py`, `test_walking_skeleton.py`, or `test_live_stack.py`.

Then run the whole suite (not a subset — the key rename is global; `test_retrieve.py` is a
Docker-free producer unit test that would otherwise commit red unnoticed):

Run: `KDIVE_REQUIRE_DOCKER=1 just test`
Expected: PASS. A *skipped* DB test does **not** satisfy this step — `KDIVE_REQUIRE_DOCKER=1` turns
an absent Docker daemon into an error so a missing backend can't masquerade as green.

- [ ] **Step 7: Guardrails + commit**

Run: `just lint && just type`
Expected: clean.

```bash
git add src/kdive/providers/local_libvirt/retrieve.py src/kdive/mcp/tools/vmcore.py src/kdive/mcp/tools/introspect.py tests/mcp/test_vmcore_tools.py tests/mcp/test_introspect_tools.py tests/providers/local_libvirt/test_retrieve.py tests/integration/test_walking_skeleton.py tests/integration/test_live_stack.py
git commit -m "refactor(vmcore): encode capture method in the raw object key

Store the raw core as vmcore-{method} (redacted: vmcore-{method}-redacted) and
move the three raw-core readers + test fixtures to match. Behavior-preserving:
still one core per System; method-aware dedup lands next.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Enforce first-method-wins in the capture handler

**Files:**
- Modify: `src/kdive/mcp/tools/vmcore.py` (`_captured_method` + `_ensure_method_match` helpers; `_precheck_system`, `_finalize_capture`, `capture_handler` signatures)
- Test: `tests/mcp/test_vmcore_tools.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/mcp/test_vmcore_tools.py` (after `test_capture_handler_idempotent_skips_recapture`):

```python
def test_capture_handler_rejects_different_method(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                    "retention_class) VALUES ('systems', %s, %s, 'e', 'sensitive', 'vmcore')",
                    (sys_id, f"local/systems/{sys_id}/vmcore-host_dump"),
                )
            job = await _enqueue_capture(pool, sys_id, method="kdump")
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await vmcore_tools.capture_handler(conn, job, _NoCaptureRetriever())
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert exc.value.details["existing_method"] == "host_dump"
            assert exc.value.details["requested_method"] == "kdump"
            assert await _artifact_count(pool, sys_id) == 1  # no second core written

    asyncio.run(_run())


def test_captured_method_rejects_bare_key() -> None:
    with pytest.raises(CategorizedError) as exc:
        vmcore_tools._captured_method("local/systems/x/vmcore")
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
```

Note: `test_capture_handler_idempotent_skips_recapture` (from Task 1) already covers same-method
idempotency with the suffixed key; no new same-method test is needed.

- [ ] **Step 2: Run to verify they fail**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/mcp/test_vmcore_tools.py::test_capture_handler_rejects_different_method tests/mcp/test_vmcore_tools.py::test_captured_method_rejects_bare_key -q`
Expected: FAIL — the reject test currently no-ops (precheck returns the host_dump key, no raise); `_captured_method` does not exist yet. (`KDIVE_REQUIRE_DOCKER=1` so the DB-backed reject test errors rather than *skips* on a host without Docker — a skip is not a "fail" and would make this TDD gate vacuous.)

- [ ] **Step 3: Add the helpers in `vmcore.py`**

Add after `_existing_raw_key`:

```python
def _captured_method(object_key: str) -> str:
    """The method suffix of a raw vmcore key (`.../vmcore-host_dump` -> `host_dump`)."""
    _, sep, method = object_key.rpartition("/vmcore-")
    if not sep or not method:
        raise CategorizedError(
            "malformed raw vmcore object key (no method suffix)",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"object_key": object_key},
        )
    return method


def _ensure_method_match(existing_key: str, method: CaptureMethod, system_id: UUID) -> None:
    """Raise `configuration_error` when an existing core was captured by a different method."""
    captured = _captured_method(existing_key)
    if captured != method.value:
        raise CategorizedError(
            "a vmcore captured via a different method already exists for this System",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "system_id": str(system_id),
                "existing_method": captured,
                "requested_method": method.value,
            },
        )
```

- [ ] **Step 4: Thread the method through precheck/finalize/handler**

`_precheck_system` (add `method` param; guard the existing key):

```python
async def _precheck_system(
    conn: AsyncConnection, system_id: UUID, method: CaptureMethod
) -> System | str:
    """Under the per-System lock, return an existing same-method key, or the System to capture.

    Raises `configuration_error` if a core from a different method already exists (first method
    wins) — before the slow `capture()` seam, so the common case writes no object.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "capture target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        existing = await _existing_raw_key(conn, system_id)
        if existing is not None:
            _ensure_method_match(existing, method, system_id)
            return existing
        return system
```

`_finalize_capture` (add `method` param; guard the concurrent-win key):

```python
async def _finalize_capture(
    conn: AsyncConnection, job: Job, system: System, method: CaptureMethod, output: Any
) -> str:
    """Insert both artifact rows + audit under the per-System lock; tolerate a concurrent win."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system.id):
        existing = await _existing_raw_key(conn, system.id)
        if existing is not None:
            _ensure_method_match(existing, method, system.id)
            return existing
        await ARTIFACTS.insert(
            conn, register_artifact_row(output.raw, owner_kind="systems", owner_id=system.id)
        )
        await ARTIFACTS.insert(
            conn, register_artifact_row(output.redacted, owner_kind="systems", owner_id=system.id)
        )
        await audit.record(
            conn,
            _ctx_from_job(job, system.project),
            tool="vmcore.fetch",
            object_kind="systems",
            object_id=system.id,
            transition="capture_vmcore",
            args={"system_id": str(system.id)},
            project=system.project,
        )
    return str(output.raw.key)
```

`capture_handler` (pass `method` to both):

```python
    system_id = UUID(job.payload["system_id"])
    method = CaptureMethod(job.payload["method"])
    precheck = await _precheck_system(conn, system_id, method)
    if isinstance(precheck, str):
        return precheck
    output = await asyncio.to_thread(retriever.capture, system_id, method)
    return await _finalize_capture(conn, job, precheck, method, output)
```

- [ ] **Step 5: Run the new + existing handler tests**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/mcp/test_vmcore_tools.py -q`
Expected: PASS (new reject + bare-key tests pass; idempotency, store, no-core, missing-system, list, postmortem all still pass). A skip ≠ a pass — `KDIVE_REQUIRE_DOCKER=1` enforces that.

- [ ] **Step 6: Full guardrails**

Run: `just lint && just type && KDIVE_REQUIRE_DOCKER=1 just test`
Expected: clean / all pass (gated `live_vm`/`live_stack` excluded by the recipe; `KDIVE_REQUIRE_DOCKER=1` keeps the DB tests from skipping into a false green).

- [ ] **Step 7: Commit**

```bash
git add src/kdive/mcp/tools/vmcore.py tests/mcp/test_vmcore_tools.py
git commit -m "fix(vmcore): reject a second capture method per System (first wins)

The capture handler now compares the requested method against the method
encoded in any existing raw core: same method is idempotent, a different
method raises configuration_error in precheck (orphan-free) and again in the
finalize race backstop, instead of silently returning the first method's core.

Closes #118

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** producer rename (Task 1 S1), 3-reader SQL (T1 S2/S4), list filter (T1 S3),
  `_captured_method` fail-fast (T2 S3), precheck+finalize reject (T2 S4), non-vacuous redaction
  guard at all three guard sites — `test_vmcore_tools.py:471`, `test_walking_skeleton.py:295`,
  gated `test_live_stack.py:525` (T1 S5), producer-unit test `test_retrieve.py` incl. the
  name-keyed `_FakeStore(fail_on=...)` (T1 S5), producer-side fakes (T1 S5),
  different-method/bare-key/same-method tests (T2 S1, T1 idempotency). All spec sections map to a
  step; the `rg` sweep + full `KDIVE_REQUIRE_DOCKER=1 just test` (T1 S6) backstops a missed site.
- **No admission-layer change** (ADR-0050 Decision 4) — `fetch_vmcore` untouched; M0 keeps
  rejecting `kdump` at the supported-set boundary.
- **Type consistency:** `_ensure_method_match(existing_key, method, system_id)` and
  `_captured_method(object_key)` are referenced exactly as defined; `_precheck_system`/
  `_finalize_capture`/`capture_handler` all take `method: CaptureMethod`.
