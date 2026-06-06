# systems.define — create-without-provision producer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `systems.define` (the producer of `SystemState.DEFINED`) and the `defined → provisioning` admission path so the ADR-0048 rootfs-upload lane is reachable end-to-end, terminable, and leak-free.

**Architecture:** A new `define_system` tool inserts a System at `defined` for a `granted` Allocation and flips it `granted → active`; `provision_system` gains a `defined → provisioning` admission branch (profile becomes optional, stored profile wins). The #110 `upload` boundary fence splits into static well-formedness (kept on the worker render path) and a lane guard (rejects `upload` only in the one-step-provision and reprovision lanes). A `defined → torn_down` state edge makes an abandoned `DEFINED` System terminable, and `create_upload` becomes kind-aware so a non-`upload` `DEFINED` System cannot mint an orphan object.

**Tech Stack:** Python 3.13, `uv`, `pytest` (Docker-gated `migrated_url` fixture via testcontainers; not `live_vm`), FastMCP tools driven directly with an injected pool + `RequestContext`, Postgres advisory locks, `minio_store` object-store fixture.

**Spec:** `docs/superpowers/specs/2026-06-05-systems-define-create-without-provision-design.md`.

**Conventions:**
- Run a single test: `uv run python -m pytest <path>::<name> -q`.
- Guardrail gate before every commit: `just lint && just type && just test` (CI runs these recipes individually; `just type` is whole-tree — src **and** tests).
- Commit trailer: every commit ends with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Conventional-commit subjects, imperative, ≤72 chars.

---

## File Structure

- `src/kdive/domain/state.py` — add the `DEFINED → TORN_DOWN` edge (Task 1).
- `src/kdive/providers/local_libvirt/provisioning.py` — split `validate_rootfs_reference` (static-only) from a new `reject_rootfs_without_upload_window` lane guard; refresh the `resolve_rootfs_path` upload comment (Task 2).
- `src/kdive/mcp/tools/systems.py` — `reprovision` upload reject (Task 3); `define_system` + `_define_locked` + `_defined_envelope` + `systems.define` registration (Task 4); `provision_system` optional profile + admit-`defined` branch + create-lane upload reject (Task 5).
- `src/kdive/mcp/tools/artifacts.py` — kind-aware `_owner_accepts_upload` (Task 6).
- `tests/domain/test_state.py` — LEGAL-table edge (Task 1).
- `tests/providers/local_libvirt/test_rootfs_resolve.py` — flip the upload-rejection test to the new guard (Task 2).
- `tests/mcp/test_systems_tools.py` — reprovision/define/provision/teardown tests (Tasks 3,4,5,7); refresh the stale `#111` comment.
- `tests/mcp/test_create_upload_tool.py` — kind-aware tests + rewrite `DEFINED` seeds (Tasks 6,9).
- `tests/reconciler/test_upload_reaper.py` — rewrite `DEFINED` seeds (Task 9).
- `tests/integration/test_systems_define_upload_provision.py` — new E2E reachability test (Task 8).
- Consumer comment hygiene across `artifacts.py`, `systems.py`, `reconciler/loop.py`, `profiles/provisioning.py`, `providers/local_libvirt/provisioning.py` (Task 10).

---

## Task 1: State edge `defined → torn_down`

**Files:**
- Modify: `src/kdive/domain/state.py:150`
- Test: `tests/domain/test_state.py:55`

- [ ] **Step 1: Add the edge to the LEGAL test table (failing expectation first)**

In `tests/domain/test_state.py`, change the `SystemState.DEFINED` row (line 55) from:

```python
        SystemState.DEFINED: {SystemState.PROVISIONING, SystemState.FAILED},
```

to:

```python
        SystemState.DEFINED: {
            SystemState.PROVISIONING,
            SystemState.TORN_DOWN,
            SystemState.FAILED,
        },
```

- [ ] **Step 2: Run the state tests to verify the new edge fails against the source table**

Run: `uv run python -m pytest tests/domain/test_state.py -q`
Expected: FAIL — `test_legal_transitions_are_allowed[SystemState.defined->torn_down]` asserts `can_transition(DEFINED, TORN_DOWN)` is `True`, but the source table still rejects it.

- [ ] **Step 3: Add the edge to the source adjacency table**

In `src/kdive/domain/state.py`, change line 150 from:

```python
        SystemState.DEFINED: frozenset({SystemState.PROVISIONING, SystemState.FAILED}),
```

to:

```python
        SystemState.DEFINED: frozenset(
            {SystemState.PROVISIONING, SystemState.TORN_DOWN, SystemState.FAILED}
        ),
```

- [ ] **Step 4: Update the `SystemState` docstring to record the new edge**

In `src/kdive/domain/state.py`, the `SystemState` class docstring (around line 80) currently documents M1's reprovision cycle. Append a sentence:

```python
    M1 reprovision-in-place (ADR-0038) cycles a ready System through
    ``ready → reprovisioning → ready`` on the same row; an interrupted reprovision
    fails to ``reprovisioning → failed``. ``defined → torn_down`` (ADR-0025 decision 10,
    #111) lets an abandoned create-without-provision System be torn down without first
    advancing to ``provisioning``.
```

- [ ] **Step 5: Run the state tests to verify they pass**

Run: `uv run python -m pytest tests/domain/test_state.py -q`
Expected: PASS.

- [ ] **Step 6: Guardrails + commit**

Run: `just lint && just type && just test`
Expected: all green.

```bash
git add src/kdive/domain/state.py tests/domain/test_state.py
git commit -m "feat(state): allow defined -> torn_down so a DEFINED System is terminable

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Split static rootfs validation from the upload-lane guard

**Files:**
- Modify: `src/kdive/providers/local_libvirt/provisioning.py:129-160` (`validate_rootfs_reference`), add `reject_rootfs_without_upload_window`, refresh `resolve_rootfs_path:107-111`
- Test: `tests/providers/local_libvirt/test_rootfs_resolve.py:43-48`

- [ ] **Step 1: Rewrite the upload-rejection test to target the new lane guard**

In `tests/providers/local_libvirt/test_rootfs_resolve.py`, replace the import line 11-14 and the test at lines 43-48.

Imports become:

```python
from kdive.providers.local_libvirt.provisioning import (
    reject_rootfs_without_upload_window,
    resolve_rootfs_path,
    validate_rootfs_reference,
)
```

Replace `test_validate_rootfs_reference_rejects_upload_until_producer_lands` with:

```python
def test_validate_rootfs_reference_accepts_well_formed_upload() -> None:
    # upload is well-formed (no fields to check); the worker's render path must accept it
    # so an admitted DEFINED System can render (#111). Lane admissibility is a separate guard.
    validate_rootfs_reference(_UploadRootfs(kind="upload"))  # does not raise


def test_reject_rootfs_without_upload_window_rejects_upload() -> None:
    # The one-step provision / reprovision lanes have no upload window, so an upload
    # reference there can never have a staged object — fail fast (#111).
    with pytest.raises(CategorizedError) as e:
        reject_rootfs_without_upload_window(_UploadRootfs(kind="upload"))
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_reject_rootfs_without_upload_window_allows_path() -> None:
    reject_rootfs_without_upload_window(_PathRootfs(kind="path", path="/img/x.qcow2"))  # no raise
```

- [ ] **Step 2: Run the resolver tests to verify the new shape fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_rootfs_resolve.py -q`
Expected: FAIL — `reject_rootfs_without_upload_window` is undefined (ImportError) and `validate_rootfs_reference(upload)` still raises.

- [ ] **Step 3: Make `validate_rootfs_reference` static-only and add the lane guard**

In `src/kdive/providers/local_libvirt/provisioning.py`, replace the whole `validate_rootfs_reference` function (lines 129-160) with:

```python
def validate_rootfs_reference(rootfs: RootfsSource) -> None:
    """Validate a rootfs reference's *static* well-formedness (a synchronous boundary check).

    Mirrors :func:`resolve_rootfs_path`'s static checks (url sha256 format, catalog-name
    existence) but needs no ``system_id`` — so the provisioning tool boundary and the worker's
    ``render_domain_xml`` reject a syntactically broken reference as ``configuration_error``
    instead of dead-lettering the provision job. ``path``/``upload`` carry nothing to check;
    an ``upload`` is well-formed here so the worker can render an admitted ``DEFINED`` System's
    upload rootfs. Lane admissibility (an ``upload`` needs a prior upload window) is a separate
    concern — see :func:`reject_rootfs_without_upload_window`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a malformed url checksum or unknown
            catalog name.
    """
    if rootfs.kind == "url" and not _SHA256.match(rootfs.sha256):
        raise CategorizedError(
            "rootfs url sha256 must be 'sha256:<64-hex>'",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if rootfs.kind == "catalog" and load_catalog().lookup(rootfs.name) is None:
        raise CategorizedError(
            f"unknown rootfs catalog name: {rootfs.name}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": rootfs.name},
        )


def reject_rootfs_without_upload_window(rootfs: RootfsSource) -> None:
    """Reject an ``upload`` rootfs in a lane that has no pre-provision upload window.

    An ``upload`` rootfs resolves a System-owned object that exists only after
    ``systems.define`` opens an upload window and the agent PUTs it (ADR-0048 §5). The
    one-step ``systems.provision`` *create* lane and ``systems.reprovision`` have no such
    window, so an ``upload`` reference there can never have a staged object — fail fast at the
    boundary rather than insert/replace and dead-letter (or leak a started domain) at commit.
    ``define`` and the worker do **not** call this guard.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an ``upload`` rootfs.
    """
    if rootfs.kind == "upload":
        raise CategorizedError(
            "rootfs 'upload' kind requires systems.define + artifacts.create_upload first; "
            "use 'path', 'url', or 'catalog' for a one-step provision",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
```

- [ ] **Step 4: Refresh the `resolve_rootfs_path` upload comment**

In `src/kdive/providers/local_libvirt/provisioning.py`, replace the upload branch comment at lines 107-111:

```python
    if rootfs.kind == "upload":
        # The System-owned uploaded object's local staging path. The object is committed
        # (its artifacts row written) at provisioning->ready by _commit_uploaded_rootfs;
        # staging the bytes down to this path is the install/boot spec's concern (ADR-0048 §7).
        return f"{_ROOTFS_DIR}/{tenant}-systems-{system_id}-rootfs.qcow2"
```

- [ ] **Step 5: Run the resolver tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_rootfs_resolve.py -q`
Expected: PASS.

- [ ] **Step 6: Guardrails + commit**

Run: `just lint && just type && just test`
Expected: all green (the worker handler tests `test_provision_handler_commits_uploaded_rootfs_artifact` use the fake provider and do not call `validate_rootfs_reference`, so they are unaffected).

```bash
git add src/kdive/providers/local_libvirt/provisioning.py tests/providers/local_libvirt/test_rootfs_resolve.py
git commit -m "refactor(provisioning): split static rootfs check from upload-lane guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Reprovision rejects an `upload` rootfs at the boundary

`validate_profile` no longer rejects `upload` (Task 2), so `reprovision_system` must reject it explicitly — a `ready` System has no upload window.

**Files:**
- Modify: `src/kdive/mcp/tools/systems.py` — the `from kdive.providers.local_libvirt.provisioning import (...)` block, and the validation block inside `reprovision_system`. (Anchor edits on these snippets, not line numbers — earlier tasks shift line numbers in this file.)
- Test: `tests/mcp/test_systems_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/mcp/test_systems_tools.py`:

```python
def test_reprovision_rejects_upload_rootfs(migrated_url: str) -> None:
    # A ready System has no upload window; an upload-kind reprovision is a fail-fast
    # configuration_error (#111).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            profile = _upload_profile()
            profile["provider"]["local-libvirt"]["destructive_ops"] = ["reprovision"]
            resp = await systems_tools.reprovision_system(
                pool, _ctx(), system_id=sys_id, profile=profile
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py::test_reprovision_rejects_upload_rootfs -q`
Expected: FAIL — without the guard, validation passes and the System advances toward `reprovisioning` (status not `error`).

- [ ] **Step 3: Import the guard and call it in `reprovision_system`**

In `src/kdive/mcp/tools/systems.py`, extend the `from kdive.providers.local_libvirt.provisioning import (...)` block to add `reject_rootfs_without_upload_window`:

```python
from kdive.providers.local_libvirt.provisioning import (
    LocalLibvirtProvisioning,
    Provisioner,
    domain_name_for,
    reject_rootfs_without_upload_window,
    validate_profile,
)
```

In `reprovision_system`, change its validation block (the `try:` that parses + `validate_profile`s the profile and returns `ToolResponse.failure(system_id, exc.category)`) from:

```python
    try:
        parsed = ProvisioningProfile.parse(profile)
        validate_profile(parsed)
    except CategorizedError as exc:
        return ToolResponse.failure(system_id, exc.category)
```

to:

```python
    try:
        parsed = ProvisioningProfile.parse(profile)
        validate_profile(parsed)
        reject_rootfs_without_upload_window(parsed.provider.local_libvirt.rootfs)
    except CategorizedError as exc:
        return ToolResponse.failure(system_id, exc.category)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py::test_reprovision_rejects_upload_rootfs -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type && just test`
Expected: green.

```bash
git add src/kdive/mcp/tools/systems.py tests/mcp/test_systems_tools.py
git commit -m "feat(systems): reject upload-kind rootfs at the reprovision boundary

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `systems.define` — insert `DEFINED`, flip `granted → active`

**Files:**
- Modify: `src/kdive/mcp/tools/systems.py` (add `_defined_envelope`, `define_system`, `_define_locked`, register `systems.define`)
- Test: `tests/mcp/test_systems_tools.py`

- [ ] **Step 1: Write the failing happy-path test**

Append to `tests/mcp/test_systems_tools.py`:

```python
async def _define(pool: AsyncConnectionPool, ctx: RequestContext, alloc_id: str, profile):
    return await systems_tools.define_system(pool, ctx, allocation_id=alloc_id, profile=profile)


def test_define_inserts_defined_system_and_activates_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await _define(pool, _ctx(), alloc_id, _upload_profile())
            assert resp.status == "defined"
            assert resp.suggested_next_actions == ["artifacts.create_upload", "systems.provision"]
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state, allocation_id FROM systems")
                sys_row = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition IN "
                    "('->defined', 'granted->active')"
                )
                audit_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "defined"
        assert str(sys_row["allocation_id"]) == alloc_id
        assert alloc_row is not None and alloc_row["state"] == "active"
        assert audit_row is not None and audit_row["n"] == 2

    asyncio.run(_run())
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py::test_define_inserts_defined_system_and_activates_allocation -q`
Expected: FAIL — `systems_tools.define_system` does not exist (AttributeError).

- [ ] **Step 3: Implement `_defined_envelope`, `define_system`, `_define_locked`**

In `src/kdive/mcp/tools/systems.py`, add immediately after the `_envelope_for_system` function:

```python
def _defined_envelope(system: System) -> ToolResponse:
    """Render a freshly-defined System: status ``defined``, pointing at the upload window."""
    return ToolResponse.success(
        str(system.id),
        SystemState.DEFINED.value,
        suggested_next_actions=["artifacts.create_upload", "systems.provision"],
        data={"project": system.project},
    )
```

Add, immediately after the `_provision_locked` function (its body ends with `return _system_job_envelope(job, system.id)`), the `define` tool:

```python
async def define_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    allocation_id: str,
    profile: dict[str, Any],
) -> ToolResponse:
    """Create a System in ``defined`` for a ``granted`` Allocation (ADR-0025 decision 10).

    The create-without-provision producer: it opens the rootfs-upload window (ADR-0048 §5).
    Validates the profile (``upload`` rootfs is admitted here — this is the one tool that
    opens an upload window), then under the per-allocation lock inserts the System at
    ``defined`` and flips the Allocation ``granted -> active``. Operator only. Returns a
    System envelope (no job — define does no provider work).
    """
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    try:
        parsed = ProvisioningProfile.parse(profile)
        validate_profile(parsed)
    except CategorizedError as exc:
        return ToolResponse.failure(allocation_id, exc.category)
    with bind_context(principal=ctx.principal):
        try:
            return await _define_locked(pool, ctx, uid, parsed)
        except IllegalTransition:
            async with pool.connection() as conn:
                latest = await ALLOCATIONS.get(conn, uid)
            data = {"current_status": latest.state.value} if latest else {}
            return _config_error(allocation_id, data=data)


async def _define_locked(
    pool: AsyncConnectionPool, ctx: RequestContext, alloc_id: UUID, profile: ProvisioningProfile
) -> ToolResponse:
    """Insert a ``defined`` System and flip the Allocation active, under PROJECT->ALLOCATION."""
    async with pool.connection() as probe:
        probe_alloc = await ALLOCATIONS.get(probe, alloc_id)
    if probe_alloc is None or probe_alloc.project not in ctx.projects:
        return _config_error(str(alloc_id))
    project = probe_alloc.project
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, alloc_id),
    ):
        alloc = await ALLOCATIONS.get(conn, alloc_id)
        if alloc is None or alloc.project not in ctx.projects:
            return _config_error(str(alloc_id))
        require_role(ctx, alloc.project, Role.OPERATOR)
        existing = await _find_system_for_allocation(conn, alloc_id)
        if existing is not None:
            if existing.state is SystemState.DEFINED:
                return _defined_envelope(existing)  # idempotent re-define
            return _config_error(str(existing.id), data={"current_status": existing.state.value})
        if alloc.state is not AllocationState.GRANTED:
            return _config_error(str(alloc_id), data={"current_status": alloc.state.value})
        if not await _within_system_quota(conn, alloc.project):
            return ToolResponse.failure(
                str(alloc_id),
                ErrorCategory.QUOTA_EXCEEDED,
                suggested_next_actions=["systems.get", "allocations.list"],
            )
        now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=now,
                updated_at=now,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=alloc.project,
                allocation_id=alloc_id,
                state=SystemState.DEFINED,
                provisioning_profile=profile.model_dump(by_alias=True),
            ),
        )
        await audit.record(
            conn,
            ctx,
            tool="systems.define",
            object_kind="systems",
            object_id=system.id,
            transition="->defined",
            args={"allocation_id": str(alloc_id)},
            project=alloc.project,
        )
        await ALLOCATIONS.update_state(conn, alloc_id, AllocationState.ACTIVE)
        await audit.record(
            conn,
            ctx,
            tool="systems.define",
            object_kind="allocations",
            object_id=alloc_id,
            transition="granted->active",
            args={"allocation_id": str(alloc_id)},
            project=alloc.project,
        )
        return _defined_envelope(system)
```

- [ ] **Step 4: Register the `systems.define` tool**

In `src/kdive/mcp/tools/systems.py` `register()`, add before the existing `@app.tool(name="systems.provision", ...)` block:

```python
    @app.tool(
        name="systems.define",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def systems_define(
        allocation_id: Annotated[
            str, Field(description="Granted Allocation to create a DEFINED System for.")
        ],
        profile: Annotated[
            dict[str, Any],
            Field(
                description="Provisioning profile for the System; an 'upload' rootfs opens a "
                "pre-provision rootfs-upload window."
            ),
        ],
    ) -> ToolResponse:
        """Create a System in 'defined' for a granted Allocation (upload window). Operator only."""
        return await define_system(
            pool, current_context(), allocation_id=allocation_id, profile=profile
        )
```

- [ ] **Step 5: Run the happy-path test to verify it passes**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py::test_define_inserts_defined_system_and_activates_allocation -q`
Expected: PASS.

- [ ] **Step 6: Add edge/error tests**

Append to `tests/mcp/test_systems_tools.py`:

```python
def test_define_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            first = await _define(pool, _ctx(), alloc_id, _upload_profile())
            second = await _define(pool, _ctx(), alloc_id, _upload_profile())
            assert first.object_id == second.object_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
        assert sys_n is not None and sys_n["n"] == 1  # one System
        assert alloc_row is not None and alloc_row["state"] == "active"  # not re-flipped

    asyncio.run(_run())


def test_define_non_granted_allocation_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.RELEASING)
            resp = await _define(pool, _ctx(), alloc_id, _upload_profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "releasing"

    asyncio.run(_run())


def test_define_existing_non_defined_system_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _define(pool, _ctx(), alloc_id, _upload_profile())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "ready"
        assert sys_n is not None and sys_n["n"] == 1  # no second System minted

    asyncio.run(_run())


def test_define_over_quota_is_quota_exceeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool, systems_quota=0)
            resp = await _define(pool, _ctx(), alloc_id, _upload_profile())
        assert resp.status == "error"
        assert resp.error_category == "quota_exceeded"

    asyncio.run(_run())


def test_define_requires_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            with pytest.raises(Exception):  # require_role raises AuthorizationError
                await _define(pool, _ctx(role=None), alloc_id, _upload_profile())

    asyncio.run(_run())


def test_define_foreign_allocation_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await _define(pool, _ctx(projects=("other",)), alloc_id, _upload_profile())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())
```

- [ ] **Step 7: Run the define tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k define -q`
Expected: PASS (all define tests).

- [ ] **Step 8: Guardrails + commit**

Run: `just lint && just type && just test`
Expected: green.

```bash
git add src/kdive/mcp/tools/systems.py tests/mcp/test_systems_tools.py
git commit -m "feat(systems): add systems.define producing a DEFINED System

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `systems.provision` admits a `DEFINED` System (optional profile)

**Files:**
- Modify: `src/kdive/mcp/tools/systems.py` (`provision_system` signature + `_provision_locked` branches; add `_admit_defined`; tool wrapper optional `profile`)
- Test: `tests/mcp/test_systems_tools.py`

- [ ] **Step 1: Write the failing admit test**

Append to `tests/mcp/test_systems_tools.py`:

```python
def test_provision_admits_defined_system_without_profile(migrated_url: str) -> None:
    # systems.provision(allocation_id) with no profile drives an existing DEFINED System
    # defined -> provisioning and enqueues its provision job (#111).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = (await _define(pool, _ctx(), alloc_id, _upload_profile())).object_id
            resp = await systems_tools.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=None
            )
            assert resp.status == "queued"
            assert resp.data["system_id"] == sys_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
                alloc_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'defined->provisioning'"
                )
                audit_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "provisioning"
        assert alloc_row is not None and alloc_row["state"] == "active"  # untouched (flipped at define)
        assert audit_row is not None and audit_row["n"] == 1

    asyncio.run(_run())


def test_provision_create_lane_rejects_upload(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await systems_tools.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=_upload_profile()
            )
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert sys_n is not None and sys_n["n"] == 0  # fail fast, no System inserted

    asyncio.run(_run())


def test_provision_create_lane_requires_profile(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            resp = await systems_tools.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=None
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k "admits_defined or create_lane" -q`
Expected: FAIL — `provision_system` rejects `profile=None` (TypeError/validation) and there is no admit branch.

- [ ] **Step 3: Make `provision_system`'s profile optional and validate conditionally**

In `src/kdive/mcp/tools/systems.py`, replace the entire `provision_system` function (the `async def provision_system(...)` whose `except IllegalTransition:` returns `_config_error(allocation_id, data=data)`) with:

```python
async def provision_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    allocation_id: str,
    profile: dict[str, Any] | None,
) -> ToolResponse:
    """Mint or admit a System for a ``granted`` Allocation and enqueue its provision job.

    Create lane (no System yet): ``profile`` is required; an ``upload`` rootfs is rejected
    (no upload window). Admit lane (a ``defined`` System exists): ``profile`` is ignored and
    the stored profile is provisioned (ADR-0025 decisions 7, 10).
    """
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    parsed: ProvisioningProfile | None = None
    if profile is not None:
        try:
            parsed = ProvisioningProfile.parse(profile)
            validate_profile(parsed)
        except CategorizedError as exc:
            return ToolResponse.failure(allocation_id, exc.category)
    with bind_context(principal=ctx.principal):
        try:
            return await _provision_locked(pool, ctx, uid, parsed)
        except IllegalTransition:
            async with pool.connection() as conn:
                latest = await ALLOCATIONS.get(conn, uid)
            data = {"current_status": latest.state.value} if latest else {}
            return _config_error(allocation_id, data=data)
```

- [ ] **Step 4: Add the admit-`defined` branch and create-lane guards in `_provision_locked`**

In `src/kdive/mcp/tools/systems.py`, change `_provision_locked`'s signature (the `async def _provision_locked(...)` currently typed `profile: ProvisioningProfile`) so `profile` is optional:

```python
async def _provision_locked(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    alloc_id: UUID,
    profile: ProvisioningProfile | None,
) -> ToolResponse:
```

Inside `_provision_locked`, replace the existing-System branch and the create block — from the `existing = await _find_system_for_allocation(conn, alloc_id)` line down to and including the `SYSTEMS.insert(...)` that constructs the `System(... state=SystemState.PROVISIONING ...)` — with:

```python
        existing = await _find_system_for_allocation(conn, alloc_id)
        if existing is not None:
            if existing.state in _TERMINAL_SYSTEM:
                return _config_error(
                    str(existing.id), data={"current_status": existing.state.value}
                )
            if existing.state is SystemState.DEFINED:
                return await _admit_defined(conn, ctx, alloc, existing)
            job = await queue.enqueue(
                conn,
                JobKind.PROVISION,
                {"system_id": str(existing.id)},
                _authorizing(ctx, alloc.project),
                f"{alloc_id}:provision",
            )
            return _system_job_envelope(job, existing.id)
        if profile is None:
            return _config_error(str(alloc_id), data={"reason": "profile_required"})
        try:
            reject_rootfs_without_upload_window(profile.provider.local_libvirt.rootfs)
        except CategorizedError as exc:
            return ToolResponse.failure(str(alloc_id), exc.category)
        if alloc.state is not AllocationState.GRANTED:
            return _config_error(str(alloc_id), data={"current_status": alloc.state.value})
        # New System: enforce the per-project max_concurrent_systems quota under the held
        # project lock. Fail-closed — no quota row → denied (ADR-0007 §4); a denial writes
        # no System, no job, and leaves the allocation granted (the all-or-nothing rule).
        if not await _within_system_quota(conn, alloc.project):
            return ToolResponse.failure(
                str(alloc_id),
                ErrorCategory.QUOTA_EXCEEDED,
                suggested_next_actions=["systems.get", "allocations.list"],
            )
        now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=now,
                updated_at=now,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=alloc.project,
                allocation_id=alloc_id,
                state=SystemState.PROVISIONING,
                provisioning_profile=profile.model_dump(by_alias=True),
            ),
        )
```

Then add `_admit_defined` immediately after the `_provision_locked` function:

```python
async def _admit_defined(
    conn: AsyncConnection, ctx: RequestContext, alloc: Allocation, system: System
) -> ToolResponse:
    """Drive a ``defined`` System ``defined -> provisioning`` and enqueue its provision job.

    The stored profile is provisioned (ADR-0025 decision 7); the Allocation is already
    ``active`` (flipped at ``define``), so it is not touched. Keyed on the allocation, like
    the create lane, so a retried ``systems.provision`` dedups to the same job.
    """
    await SYSTEMS.update_state(conn, system.id, SystemState.PROVISIONING)
    await audit.record(
        conn,
        ctx,
        tool="systems.provision",
        object_kind="systems",
        object_id=system.id,
        transition="defined->provisioning",
        args={"allocation_id": str(alloc.id)},
        project=alloc.project,
    )
    job = await queue.enqueue(
        conn,
        JobKind.PROVISION,
        {"system_id": str(system.id)},
        _authorizing(ctx, alloc.project),
        f"{alloc.id}:provision",
    )
    return _system_job_envelope(job, system.id)
```

Ensure `Allocation` is imported in `systems.py` (add it to the existing `from kdive.domain.models import (...)` list if absent).

- [ ] **Step 5: Make the `systems.provision` tool wrapper's `profile` optional**

In `register()`'s `systems_provision` wrapper (the `@app.tool(name="systems.provision", ...)` inner function), change the `profile` parameter to default `None`:

```python
    async def systems_provision(
        allocation_id: Annotated[
            str, Field(description="Granted Allocation to provision a System for.")
        ],
        profile: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description="Provisioning profile for the create lane (required when no System "
                "exists yet); ignored when admitting an already-defined System.",
            ),
        ] = None,
    ) -> ToolResponse:
        """Mint or admit a System for a granted Allocation and enqueue provision. Operator only."""
        return await provision_system(
            pool, current_context(), allocation_id=allocation_id, profile=profile
        )
```

- [ ] **Step 6: Run the admit/create tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k "admits_defined or create_lane or provision" -q`
Expected: PASS (new tests plus the pre-existing `test_provision_*` still green — the create lane is unchanged for `path` profiles).

- [ ] **Step 7: Guardrails + commit**

Run: `just lint && just type && just test`
Expected: green.

```bash
git add src/kdive/mcp/tools/systems.py tests/mcp/test_systems_tools.py
git commit -m "feat(systems): admit a DEFINED System on provision; optional profile

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Kind-aware `create_upload` for `DEFINED` Systems

**Files:**
- Modify: `src/kdive/mcp/tools/artifacts.py:34` (import), `108-119` (`_owner_accepts_upload`)
- Test: `tests/mcp/test_create_upload_tool.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/mcp/test_create_upload_tool.py` (it already imports `SystemState`, `System`, `SYSTEMS`):

```python
def test_create_upload_rejects_non_upload_kind_defined_system(migrated_url: str) -> None:
    # A DEFINED System whose stored profile is path-kind cannot open an upload window —
    # else the object would be minted, never committed, and orphaned past the reaper (#111).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _seed_system(pool, state=SystemState.DEFINED)  # path-kind profile
            store = _FakeStore()
            responses = await artifacts_tools.create_upload(
                pool,
                _ctx(),
                owner_kind="system",
                owner_id=sys_id,
                artifacts=[{"name": "rootfs", "sha256": "sha256:x", "size_bytes": 10}],
                store=store,
            )
        assert len(responses) == 1
        assert responses[0].error_category == "configuration_error"
        assert responses[0].data["reason"] == "owner_not_accepting_upload"
        assert store.calls == []  # no PUT minted

    asyncio.run(_run())
```

Note: `_seed_system` in this file stores `provisioning_profile={"schema_version": 1}` — extend it so the rootfs kind is controllable. Change `_seed_system` (around line 80-125) to accept a `provisioning_profile` argument with a default upload profile, and add an `_upload_profile()`/`_path_profile()` helper near the top of the file:

```python
def _provisioning_profile(rootfs_kind: str) -> dict[str, Any]:
    rootfs: dict[str, Any] = {"kind": rootfs_kind}
    if rootfs_kind == "path":
        rootfs["path"] = "/img/x.qcow2"
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 1,
        "memory_mb": 1024,
        "disk_gb": 10,
        "boot_method": "direct-kernel",
        "kernel_source_ref": "git+https://example/linux.git#v6.9",
        "provider": {"local-libvirt": {"rootfs": rootfs, "crashkernel": "256M"}},
    }
```

and in `_seed_system`, replace `provisioning_profile={"schema_version": 1}` (line ~122) with a parameter:

```python
async def _seed_system(
    pool: AsyncConnectionPool,
    *,
    project: str = "proj",
    state: SystemState = SystemState.READY,
    rootfs_kind: str = "upload",
) -> str:
    ...
                provisioning_profile=_provisioning_profile(rootfs_kind),
    ...
```

The existing `test_create_upload_for_defined_system_*` test (which seeds `state=SystemState.DEFINED`) then defaults to `rootfs_kind="upload"` and keeps passing; the new test passes `rootfs_kind="path"`.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_create_upload_tool.py::test_create_upload_rejects_non_upload_kind_defined_system -q`
Expected: FAIL — `_owner_accepts_upload` ignores rootfs kind, so the path-kind System is accepted and a PUT is minted.

- [ ] **Step 3: Make `_owner_accepts_upload` kind-aware**

In `src/kdive/mcp/tools/artifacts.py`, add the import (after line 34's `from kdive.profiles.build import ...`):

```python
from kdive.profiles.provisioning import ProvisioningProfile
```

Replace the System branch of `_owner_accepts_upload` (lines 116-119):

```python
    # A System opens a rootfs-upload window only in DEFINED with an upload-kind rootfs;
    # the provisioning plane commits it at provisioning->ready (#111, ADR-0048 §5/§6).
    system = await SYSTEMS.get(conn, owner_id)
    if system is None or system.state is not SystemState.DEFINED:
        return False
    parsed = ProvisioningProfile.parse(system.provisioning_profile)
    return parsed.provider.local_libvirt.rootfs.kind == "upload"
```

- [ ] **Step 4: Run the create_upload tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/test_create_upload_tool.py -q`
Expected: PASS (the new reject test plus the existing `for_defined_system` test, now upload-kind by default).

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type && just test`
Expected: green.

```bash
git add src/kdive/mcp/tools/artifacts.py tests/mcp/test_create_upload_tool.py
git commit -m "feat(artifacts): admit a System upload only for an upload-kind DEFINED System

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Tear down a `DEFINED` System (admin + reconciler GC)

**Files:**
- Test only: `tests/mcp/test_systems_tools.py`

- [ ] **Step 1: Write the failing admin-teardown test**

Append to `tests/mcp/test_systems_tools.py`:

```python
def test_teardown_handler_drives_defined_system_to_torn_down(migrated_url: str) -> None:
    # An abandoned DEFINED System (no domain) is terminable via defined -> torn_down (#111).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = (await _define(pool, _ctx(), alloc_id, _upload_profile())).object_id
            job = await _enqueue_teardown(pool, sys_id)
            prov = _FakeProvisioning()
            async with pool.connection() as conn:
                await systems_tools.teardown_handler(conn, job, prov)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
        assert sys_row is not None and sys_row["state"] == "torn_down"
        assert prov.torn_down == [f"kdive-{sys_id}"]  # best-effort destroy of the absent domain

    asyncio.run(_run())


def test_reconciler_gc_tears_down_defined_orphan(migrated_url: str) -> None:
    # Releasing the allocation orphans its DEFINED System; the reconciler enqueues a teardown
    # the handler can now complete (defined -> torn_down), freeing the quota slot (#111).
    from kdive.reconciler.loop import _repair_orphaned_systems

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = (await _define(pool, _ctx(), alloc_id, _upload_profile())).object_id
            async with pool.connection() as conn:
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.RELEASING)
                await ALLOCATIONS.update_state(conn, UUID(alloc_id), AllocationState.RELEASED)
                enqueued = await _repair_orphaned_systems(conn)
            assert enqueued == 1
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s", (f"{sys_id}:teardown",)
                )
                job_n = await cur.fetchone()
        assert job_n is not None and job_n["n"] == 1

    asyncio.run(_run())
```

- [ ] **Step 2: Run them to verify they pass (the source change landed in Task 1)**

Run: `uv run python -m pytest tests/mcp/test_systems_tools.py -k "defined_system_to_torn_down or gc_tears_down_defined" -q`
Expected: PASS — Task 1 added the `defined → torn_down` edge, so these are regression-locks for the new behavior. (If either FAILs, the edge or the orphan predicate is wrong — fix before continuing.)

- [ ] **Step 3: Guardrails + commit**

Run: `just lint && just type && just test`
Expected: green.

```bash
git add tests/mcp/test_systems_tools.py
git commit -m "test(systems): pin teardown of an abandoned DEFINED System

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: End-to-end reachability test (define → upload → provision → handler)

**Files:**
- Create: `tests/integration/test_systems_define_upload_provision.py`

- [ ] **Step 1: Write the reachability test**

Create `tests/integration/test_systems_define_upload_provision.py`:

```python
"""End-to-end reachability of the rootfs-upload lane (define -> upload -> provision, #111).

DB/tool-lane reachability under a fake provider: it proves the upload-kind profile flows
through systems.define, artifacts.create_upload, systems.provision, and the provision
handler's _commit_uploaded_rootfs. It does NOT boot — staging the object to the libvirt
disk is the install/boot spec's concern (ADR-0048 §7).
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from psycopg.rows import dict_row

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.models import Sensitivity
from kdive.domain.state import AllocationState
from kdive.mcp.tools import artifacts as artifacts_tools
from kdive.mcp.tools import systems as systems_tools
from kdive.store.objectstore import ObjectStore, artifact_key
from tests.mcp.test_systems_tools import (
    _ctx,
    _define,
    _enqueue_provision,
    _FakeProvisioning,
    _granted_allocation,
    _pool,
    _upload_profile,
)


def test_define_upload_provision_reaches_ready_with_committed_rootfs(
    migrated_url: str, minio_store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(systems_tools, "object_store_from_env", lambda: minio_store)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)

            # 1. define -> DEFINED, allocation granted->active
            sys_id = (await _define(pool, _ctx(), alloc_id, _upload_profile())).object_id

            # 2. create_upload opens the window (persists the manifest, mints a PUT)
            uploads = await artifacts_tools.create_upload(
                pool,
                _ctx(),
                owner_kind="system",
                owner_id=sys_id,
                artifacts=[{"name": "rootfs", "sha256": "sha256:x", "size_bytes": 18}],
                store=minio_store,
            )
            assert uploads[0].status == "upload_ready"
            assert uploads[0].suggested_next_actions == ["systems.provision"]

            # 3. the agent PUTs the qcow2 (staged directly into the store for the test)
            minio_store.put_artifact(
                "local", "systems", sys_id, "rootfs",
                data=b"rootfs-image-bytes",
                sensitivity=Sensitivity.SENSITIVE, retention_class="rootfs",
            )

            # 4. provision admits the DEFINED System (no profile re-passed)
            resp = await systems_tools.provision_system(
                pool, _ctx(), allocation_id=alloc_id, profile=None
            )
            assert resp.status == "queued"

            # 5. the provision handler drives provisioning -> ready and commits the rootfs
            job = await _enqueue_provision(pool, sys_id, alloc_id)
            async with pool.connection() as conn:
                await systems_tools.provision_handler(conn, job, _FakeProvisioning())

            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM systems WHERE id = %s", (sys_id,))
                sys_row = await cur.fetchone()
                await cur.execute(
                    "SELECT object_key, owner_kind, sensitivity FROM artifacts WHERE owner_id = %s",
                    (sys_id,),
                )
                art_rows = await cur.fetchall()
        assert sys_row is not None and sys_row["state"] == "ready"
        assert len(art_rows) == 1
        assert art_rows[0]["object_key"] == artifact_key("local", "systems", sys_id, "rootfs")
        assert art_rows[0]["owner_kind"] == "systems"
        assert art_rows[0]["sensitivity"] == "sensitive"

    asyncio.run(_run())
```

Note: the provision tool minted the `provision` job via the admit branch; step 5 re-enqueues an identical-keyed `provision` job (the existing handler-test idiom) and runs the handler directly. The dedup key `"{alloc}:provision"` makes the second enqueue idempotent.

- [ ] **Step 2: Run it to verify it passes**

Run: `uv run python -m pytest tests/integration/test_systems_define_upload_provision.py -q`
Expected: PASS (skips cleanly if Docker is unavailable, like all `migrated_url` tests).

- [ ] **Step 3: Guardrails + commit**

Run: `just lint && just type && just test`
Expected: green.

```bash
git add tests/integration/test_systems_define_upload_provision.py
git commit -m "test(integration): prove define -> upload -> provision reaches ready

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Rewrite the directly-seeded `DEFINED` fixtures to use `systems.define`

**Files:**
- Modify: `tests/mcp/test_create_upload_tool.py` (the two `_seed_system(state=DEFINED)` call sites + the stale `#111` comment)
- Modify: `tests/reconciler/test_upload_reaper.py` (the two `seed_system(system_state=DEFINED)` call sites — `test_reaps_uncommitted_objects_past_deadline_for_defined_system` and `test_exempts_committed_object` — + the stale `#111` comment)

- [ ] **Step 1: Replace the create_upload-tool `DEFINED` seeds with a `systems.define` helper**

In `tests/mcp/test_create_upload_tool.py`, add a helper that produces a `DEFINED` System through the producer (it needs a `granted` allocation + quota + the local-libvirt resource). Reuse the `systems.define` tool:

```python
from kdive.mcp.tools import systems as systems_tools


async def _defined_system_via_tool(pool: AsyncConnectionPool, *, project: str = "proj") -> str:
    # Produce a DEFINED System through systems.define (not a seeded fixture), exercising the
    # real producer (#111). Requires a granted allocation + a quota row.
    async with pool.connection() as conn:
        res = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(), created_at=_DT, updated_at=_DT, kind=ResourceKind.LOCAL_LIBVIRT,
                pool="local-libvirt", cost_class="local", status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
        await QUOTAS.upsert(
            conn,
            Quota(project=project, max_concurrent_allocations=1_000_000,
                  max_concurrent_systems=1_000_000, updated_at=_DT),
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(), created_at=_DT, updated_at=_DT, principal="user-1", project=project,
                resource_id=res.id, state=AllocationState.GRANTED,
            ),
        )
    ctx = RequestContext(
        principal="user-1", agent_session="s", projects=(project,), roles={project: Role.OPERATOR}
    )
    resp = await systems_tools.define_system(
        pool, ctx, allocation_id=str(alloc.id), profile=_provisioning_profile("upload")
    )
    return resp.object_id
```

Add the `QUOTAS`/`Quota` imports to this test file (mirroring `test_systems_tools.py`). Then in `test_create_upload_for_defined_system_mints_rootfs_and_persists` and `test_create_upload_rejects_non_rootfs_name_for_system`, replace:

```python
            sys_id = await _seed_system(pool, state=SystemState.DEFINED)
```

with:

```python
            sys_id = await _defined_system_via_tool(pool)
```

and delete the now-stale `# Seeds DEFINED directly because no producer exists yet (#111) ...` comment block above each.

- [ ] **Step 2: Replace the reaper-test `DEFINED` seeds with `systems.define`**

The reaper tests seed through a single autocommit `connect()` connection (not a pool) with an `ACTIVE` allocation, so `seed_system(seed, system_state=SystemState.DEFINED)` cannot simply be swapped for `define_system` (which needs an `AsyncConnectionPool`, a `granted` allocation, and a `quotas` row). Add a self-contained helper that opens its own short-lived pool, seeds the prerequisites, and calls the producer.

In `tests/reconciler/test_upload_reaper.py`, extend the imports:

```python
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from kdive.db.repositories import ALLOCATIONS, QUOTAS, RESOURCES
from kdive.domain.models import Allocation, Quota, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus, RunState, SystemState
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import systems as systems_tools
from kdive.security.rbac import Role
```

Add, after the `_FakeStore`/`_insert_artifact_row` helpers (around line 56), the producer helper and a valid upload profile:

```python
_DT = datetime(2026, 1, 1, tzinfo=UTC)
_UPLOAD_PROFILE: dict[str, object] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 1,
    "memory_mb": 1024,
    "disk_gb": 10,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://example/linux.git#v6.9",
    "provider": {"local-libvirt": {"rootfs": {"kind": "upload"}, "crashkernel": "256M"}},
}


async def _defined_system_via_define(url: str, *, project: str = "proj") -> UUID:
    """Produce a DEFINED System through systems.define (the real producer, #111).

    Seeds a resource + quota + granted allocation in a short-lived pool, then calls
    systems.define (insert at DEFINED, flip the allocation granted->active). Returns the id.
    """
    async with AsyncConnectionPool(url, min_size=1, max_size=2) as pool:
        async with pool.connection() as conn:
            res = await RESOURCES.insert(
                conn,
                Resource(
                    id=uuid4(), created_at=_DT, updated_at=_DT, kind=ResourceKind.LOCAL_LIBVIRT,
                    pool="local-libvirt", cost_class="local", status=ResourceStatus.AVAILABLE,
                    host_uri="qemu:///system",
                ),
            )
            await QUOTAS.upsert(
                conn,
                Quota(project=project, max_concurrent_allocations=1_000_000,
                      max_concurrent_systems=1_000_000, updated_at=_DT),
            )
            alloc = await ALLOCATIONS.insert(
                conn,
                Allocation(
                    id=uuid4(), created_at=_DT, updated_at=_DT, principal="user-1",
                    project=project, resource_id=res.id, state=AllocationState.GRANTED,
                ),
            )
        ctx = RequestContext(
            principal="user-1", agent_session="s", projects=(project,),
            roles={project: Role.OPERATOR},
        )
        resp = await systems_tools.define_system(
            pool, ctx, allocation_id=str(alloc.id), profile=_UPLOAD_PROFILE
        )
    return UUID(resp.object_id)
```

Then, at the **two** sites that seed a `DEFINED` System — `test_reaps_uncommitted_objects_past_deadline_for_defined_system` and `test_exempts_committed_object` — delete the `# Seeds DEFINED directly ... (#111)` comment and replace:

```python
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.DEFINED)
            prefix = f"local/systems/{system_id}/"
```

with (note the producer runs first, then the `seed` connection only writes the manifest / artifact row):

```python
        system_id = await _defined_system_via_define(migrated_url)
        prefix = f"local/systems/{system_id}/"
        async with await connect(migrated_url) as seed:
```

(For `test_exempts_committed_object`, the `_insert_artifact_row` + `replace_manifest` calls stay inside the `async with await connect(...) as seed:` block, now opened after the producer helper. Re-indent the manifest/artifact writes to remain under that `seed` block.)

- [ ] **Step 3: Run both rewritten test files**

Run: `uv run python -m pytest tests/mcp/test_create_upload_tool.py tests/reconciler/test_upload_reaper.py -q`
Expected: PASS — the producer, not a fixture, now puts each System in `DEFINED`.

- [ ] **Step 4: Guardrails + commit**

Run: `just lint && just type && just test`
Expected: green.

```bash
git add tests/mcp/test_create_upload_tool.py tests/reconciler/test_upload_reaper.py
git commit -m "test: produce DEFINED Systems via systems.define, not seeded fixtures

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Comment hygiene — `#111` forward-plumbing becomes live-path

**Files:**
- Modify (anchor on the named snippet, not line numbers — earlier tasks shift these files): `src/kdive/mcp/tools/artifacts.py` (the `_owner_accepts_upload` System comment & the `create_upload` `next_action` comment), `src/kdive/mcp/tools/systems.py` (the `_NON_TERMINAL_SYSTEM` `DEFINED` comment & the `_commit_uploaded_rootfs` docstring), `src/kdive/reconciler/loop.py` (the `_UPLOAD_PRE_FINALIZE` comment), `src/kdive/profiles/provisioning.py` (the `_UploadRootfs` comment), `tests/mcp/test_systems_tools.py` (the stale `#111` comment near `_upload_profile`)

- [ ] **Step 1: Rewrite each forward-plumbing comment to describe the live path**

Replace the now-false "no producer yet (#111)" / "unreachable until #111" comments. Examples:

`src/kdive/mcp/tools/artifacts.py` `_owner_accepts_upload` — already rewritten in Task 6.

`artifacts.py:232` (the `create_upload` `next_action` comment) — change:
```python
    # The 'system' arm is the DEFINED rootfs-upload lane: create the window with systems.define,
    # upload here, then systems.provision admits the System and commits the rootfs (ADR-0048 §5).
    next_action = "runs.complete_build" if owner_kind == "run" else "systems.provision"
```

`systems.py` `_NON_TERMINAL_SYSTEM` (line 170) — drop `# forward-plumbing: no producer yet (#111)`, leaving:
```python
    SystemState.DEFINED,
```

`systems.py` `_commit_uploaded_rootfs` docstring — remove the "Forward-plumbing: the provisioning tool boundary rejects an upload rootfs until the DEFINED producer lands (#111) ..." paragraph; replace with:
```python
    Reachable via the rootfs-upload lane (#111): systems.define + artifacts.create_upload open
    the window; this commits the object at provisioning->ready. The absent-object guard below
    fails a profile whose upload never landed.
```

`reconciler/loop.py:385-387` — change to:
```python
# Both arms are live: a "created" external Run (#110) and a "defined" rootfs-upload System
# (#111). Each reaps an owner's uncommitted objects once its upload deadline lapses.
_UPLOAD_PRE_FINALIZE = {"runs": "created", "systems": "defined"}
```

`profiles/provisioning.py` `_UploadRootfs` — change the comment to:
```python
class _UploadRootfs(_ProfileBase):
    # A System-owned uploaded qcow2; opened by systems.define + artifacts.create_upload and
    # committed at provisioning->ready (ADR-0048 §5, #111). path/url/catalog are the alternatives.
    kind: Literal["upload"]
```

`tests/mcp/test_systems_tools.py` — replace the stale block above `_upload_profile` ("As of the #111 gate, validate_rootfs_reference rejects kind:upload ... unreachable end-to-end until #111") with:
```python
# The upload-rootfs commit path is reachable end-to-end via the rootfs-upload lane (#111):
# systems.define + artifacts.create_upload + systems.provision. These handler tests drive the
# provisioning->ready commit directly with a seeded PROVISIONING System and the minio store.
```

- [ ] **Step 2: Verify no stale `#111` "producer" / "until #111" comments remain**

Run: `rg -n "no producer yet|until #111|unreachable.*#111|awaits its producer" src tests`
Expected: no matches (the design docs may still reference #111 historically — that is fine; this scan is `src`/`tests` only).

- [ ] **Step 3: Guardrails + commit**

Run: `just lint && just type && just test`
Expected: green.

```bash
git add src/kdive/mcp/tools/artifacts.py src/kdive/mcp/tools/systems.py src/kdive/reconciler/loop.py src/kdive/profiles/provisioning.py tests/mcp/test_systems_tools.py
git commit -m "docs(code): retire #111 forward-plumbing comments for the live upload lane

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Full-suite gate

- [ ] **Step 1: Run the complete CI gate locally**

Run: `just ci`
Expected: lint, type (whole-tree), lint-shell, lint-workflows, check-mermaid, and the full (non-`live_vm`) test suite all green.

- [ ] **Step 2: Confirm no gating was weakened**

Run: `git diff main..HEAD -- 'tests/**' | rg -n "live_vm|@pytest.mark|REQUIRE_DOCKER"`
Expected: no `live_vm` marker removed, no gate widened — only additions of new ungated tests that already skip without Docker.

---

## Self-Review (run after the plan, before execution)

**Spec coverage check (each acceptance criterion → task):**
1. `systems.define` inserts DEFINED + `granted→active`, idempotent, operator-only, quota-enforced → **Task 4**.
2. `provision(allocation_id)` admits DEFINED → ready + commits rootfs under fake provider → **Task 5** (admit) + **Task 8** (E2E commit).
3. `upload` rejected only in the no-window lanes, accepted via define, renders in worker → **Task 2** (split) + **Task 3** (reprovision) + **Task 5** (create lane) + **Task 4** (define accepts).
4. DEFINED terminable (`defined→torn_down`); `create_upload` rejects non-`upload` DEFINED → **Task 1** + **Task 7** (terminable), **Task 6** (kind-aware).
5. E2E reachability passes; two `DEFINED`-seed fixtures rewritten; `#111` comments retired → **Task 8** + **Task 9** + **Task 10**.
6. ADR-0025/0048 describe the producer + the edge → already committed in the spec/ADR phase (pre-plan).
7. `just ci` green; no gating weakened → **Task 11**.

**Type consistency check:** `define_system`/`_define_locked`/`_defined_envelope`, `_admit_defined(conn, ctx, alloc, system)`, `reject_rootfs_without_upload_window(rootfs)`, `provision_system(..., profile: dict | None)`, `_provision_locked(..., profile: ProvisioningProfile | None)`, `_owner_accepts_upload` returning kind-aware bool — names are used consistently across Tasks 2-6 and 8. `Allocation` import added to `systems.py` (Task 5). `_provisioning_profile(rootfs_kind)` / `_upload_profile()` helper names align between `test_systems_tools.py` (existing `_upload_profile`) and the new `test_create_upload_tool.py` helper.

**Placeholder scan:** no TODO/TBD/"add error handling"/"similar to Task N" — every code step shows the exact code.
