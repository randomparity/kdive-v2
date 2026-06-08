"""vmcore.* / postmortem.* tool + handler tests — handlers called directly."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, Sensitivity
from kdive.jobs import queue
from kdive.jobs.models import HandlerRegistry
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.lifecycle import vmcore as vmcore_tools
from kdive.planes import vmcore as vmcore_plane
from kdive.providers.ports import CaptureOutput, CrashOutput, CrashPostmortem
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.store.objectstore import StoredArtifact
from tests.mcp._seed import seed_crashed_system, seed_run_on_system

_AUTH = {"principal": "u", "agent_session": "s", "project": "proj"}
_TEST_CAPTURE_METHODS = frozenset({CaptureMethod.HOST_DUMP})


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _fetch_vmcore(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    method: str = "host_dump",
):
    return await _vmcore_handlers().fetch_vmcore(
        pool,
        ctx,
        system_id=system_id,
        method=method,
    )


def _capture_output(sys_id: str, method: CaptureMethod = CaptureMethod.HOST_DUMP) -> CaptureOutput:
    raw = StoredArtifact(
        f"local/systems/{sys_id}/vmcore-{method.value}", "e1", Sensitivity.SENSITIVE, "vmcore"
    )
    red = StoredArtifact(
        f"local/systems/{sys_id}/vmcore-{method.value}-redacted",
        "e2",
        Sensitivity.REDACTED,
        "vmcore",
    )
    return CaptureOutput(raw=raw, redacted=red, vmcore_build_id="deadbeef")


class _FakeRetriever:
    """Records capture calls; returns a canned CaptureOutput or raises a planted error."""

    def __init__(self, sys_id: str, *, raises: CategorizedError | None = None) -> None:
        self._sys_id = sys_id
        self._raises = raises
        self.calls = 0
        self.methods: list[CaptureMethod] = []

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        self.calls += 1
        self.methods.append(method)
        if self._raises is not None:
            raise self._raises
        return _capture_output(self._sys_id, method)


class _NoCaptureRetriever:
    """Fails the test if .capture is ever called (idempotency probe)."""

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        raise AssertionError("capture must not be called when a vmcore row already exists")


class _FakeCrash:
    """Records postmortem kwargs; returns a canned CrashOutput with a planted secret."""

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def run_crash_postmortem(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str, commands: list[str]
    ) -> CrashOutput:
        self.kwargs = {
            "vmcore_ref": vmcore_ref,
            "debuginfo_ref": debuginfo_ref,
            "expected_build_id": expected_build_id,
            "commands": commands,
        }
        return CrashOutput(
            results={c: {"ran": True} for c in commands},
            transcript="$ log\npassword=hunter2\nok",
            truncated=False,
        )


def _vmcore_handlers(crash: CrashPostmortem | None = None) -> vmcore_tools.VmcoreHandlers:
    return vmcore_tools.VmcoreHandlers(
        supported_methods=_TEST_CAPTURE_METHODS,
        crash=crash or _FakeCrash(),
    )


class _RaisingCrash:
    """A CrashPostmortem that raises a planted CategorizedError."""

    def __init__(self, category: ErrorCategory) -> None:
        self._category = category

    def run_crash_postmortem(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str, commands: list[str]
    ) -> CrashOutput:
        raise CategorizedError("planted", category=self._category)


# --- vmcore.fetch tool ---------------------------------------------------------------------


def test_fetch_vmcore_crashed_enqueues_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            resp = await _fetch_vmcore(pool, _ctx(), system_id=sys_id)
            assert resp.status == "queued"
            assert resp.data["system_id"] == sys_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE kind = 'capture_vmcore' "
                    "AND dedup_key = %s",
                    (f"{sys_id}:capture_vmcore:host_dump",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_fetch_vmcore_non_crashed_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE systems SET state = 'torn_down' WHERE id = %s", (sys_id,)
                )
            resp = await _fetch_vmcore(pool, _ctx(), system_id=sys_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "torn_down"

    asyncio.run(_run())


def test_fetch_vmcore_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            with pytest.raises(AuthorizationError):
                await _fetch_vmcore(pool, _ctx(Role.VIEWER), system_id=sys_id)

    asyncio.run(_run())


def test_fetch_vmcore_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _fetch_vmcore(pool, _ctx(), system_id="nope")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_fetch_rejects_unsupported_method(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            resp = await _fetch_vmcore(pool, _ctx(), system_id=sys_id, method="kdump")
            assert resp.status == "error"
            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_fetch_rejects_non_core_method(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            resp = await _fetch_vmcore(pool, _ctx(), system_id=sys_id, method="console")
            assert resp.status == "error"
            assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_fetch_records_method_in_dedup_key(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            resp = await _fetch_vmcore(pool, _ctx(), system_id=sys_id, method="host_dump")
            assert resp.status == "queued"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s",
                    (f"{sys_id}:capture_vmcore:host_dump",),
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())


# --- capture handler -----------------------------------------------------------------------


async def _enqueue_capture(
    pool: AsyncConnectionPool, sys_id: str, method: str = "host_dump"
) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.CAPTURE_VMCORE,
            {"system_id": sys_id, "method": method},
            _AUTH,
            f"{sys_id}:capture_vmcore:{method}",
        )


async def _artifact_count(pool: AsyncConnectionPool, sys_id: str) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT count(*) AS n FROM artifacts WHERE owner_kind = 'systems' AND owner_id = %s",
            (sys_id,),
        )
        row = await cur.fetchone()
    return 0 if row is None else int(row["n"])


def test_capture_handler_stores_rows_and_returns_ref(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            job = await _enqueue_capture(pool, sys_id)
            retriever = _FakeRetriever(sys_id)
            async with pool.connection() as conn:
                ref = await vmcore_plane.capture_handler(conn, job, retriever)
            assert ref == f"local/systems/{sys_id}/vmcore-host_dump"
            assert retriever.calls == 1
            assert await _artifact_count(pool, sys_id) == 2
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT sensitivity FROM artifacts WHERE owner_kind = 'systems' "
                    "AND owner_id = %s ORDER BY sensitivity",
                    (sys_id,),
                )
                rows = await cur.fetchall()
        assert [r["sensitivity"] for r in rows] == ["redacted", "sensitive"]

    asyncio.run(_run())


def test_capture_handler_plumbs_method_to_retriever(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            job = await _enqueue_capture(pool, sys_id, method="host_dump")
            retriever = _FakeRetriever(sys_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(conn, job, retriever)
        assert retriever.methods == [CaptureMethod.HOST_DUMP]

    asyncio.run(_run())


def test_capture_handler_idempotent_skips_recapture(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
                    "retention_class) VALUES ('systems', %s, %s, 'e', 'sensitive', 'vmcore')",
                    (sys_id, f"local/systems/{sys_id}/vmcore-host_dump"),
                )
            job = await _enqueue_capture(pool, sys_id)
            async with pool.connection() as conn:
                ref = await vmcore_plane.capture_handler(conn, job, _NoCaptureRetriever())
            assert ref == f"local/systems/{sys_id}/vmcore-host_dump"
            assert await _artifact_count(pool, sys_id) == 1  # no second row

    asyncio.run(_run())


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
                    await vmcore_plane.capture_handler(conn, job, _NoCaptureRetriever())
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert exc.value.details["existing_method"] == "host_dump"
            assert exc.value.details["requested_method"] == "kdump"
            assert await _artifact_count(pool, sys_id) == 1  # no second core written

    asyncio.run(_run())


def test_captured_method_rejects_bare_key() -> None:
    with pytest.raises(CategorizedError) as exc:
        vmcore_plane.captured_method("local/systems/x/vmcore")
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_capture_handler_no_core_raises_readiness(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            job = await _enqueue_capture(pool, sys_id)
            err = CategorizedError("no core", category=ErrorCategory.READINESS_FAILURE)
            retriever = _FakeRetriever(sys_id, raises=err)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await vmcore_plane.capture_handler(conn, job, retriever)
            assert exc.value.category is ErrorCategory.READINESS_FAILURE
            assert await _artifact_count(pool, sys_id) == 0

    asyncio.run(_run())


def test_capture_handler_missing_system_is_infra_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ghost = str(uuid4())
            job = await _enqueue_capture(pool, ghost)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await vmcore_plane.capture_handler(conn, job, _FakeRetriever(ghost))
        assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE

    asyncio.run(_run())


# --- vmcore.list ---------------------------------------------------------------------------


def test_list_vmcores_redacted_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            job = await _enqueue_capture(pool, sys_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(conn, job, _FakeRetriever(sys_id))
            resp = await vmcore_tools.list_vmcores(pool, _ctx(), system_id=sys_id)
        keys = {r.refs["object"] for r in resp.collection_items()}
        assert keys == {f"local/systems/{sys_id}/vmcore-host_dump-redacted"}

    asyncio.run(_run())


def test_list_vmcores_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            job = await _enqueue_capture(pool, sys_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(conn, job, _FakeRetriever(sys_id))
            with pytest.raises(AuthorizationError):
                await vmcore_tools.list_vmcores(pool, _ctx(role=None), system_id=sys_id)

    asyncio.run(_run())


# --- postmortem.crash ----------------------------------------------------------------------


async def _crashed_with_built_run(pool: AsyncConnectionPool) -> str:
    sys_id = await seed_crashed_system(pool)
    run_id = await seed_run_on_system(
        pool, sys_id, debuginfo_ref="k/runs/r/vmlinux", build_id="deadbeef"
    )
    job = await _enqueue_capture(pool, sys_id)
    async with pool.connection() as conn:
        await vmcore_plane.capture_handler(conn, job, _FakeRetriever(sys_id))
    return run_id


def test_postmortem_crash_bad_command_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            crash = _FakeCrash()
            resp = await _vmcore_handlers(crash).postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["bt | sh"]
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert crash.kwargs == {}  # the port was never called

    asyncio.run(_run())


def test_postmortem_crash_runs_and_redacts(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            crash = _FakeCrash()
            resp = await _vmcore_handlers(crash).postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["log"]
            )
        assert resp.status != "error"
        assert "hunter2" not in resp.data["transcript"]
        assert "[REDACTED]" in resp.data["transcript"]
        assert crash.kwargs["expected_build_id"] == "deadbeef"

    asyncio.run(_run())


def test_postmortem_crash_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            with pytest.raises(AuthorizationError):
                await _vmcore_handlers().postmortem_crash(
                    pool, _ctx(role=None), run_id=run_id, commands=["log"]
                )

    asyncio.run(_run())


def test_postmortem_crash_unbuilt_run_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            resp = await _vmcore_handlers().postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["log"]
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_postmortem_crash_provenance_mismatch_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            crash = _RaisingCrash(ErrorCategory.CONFIGURATION_ERROR)
            resp = await _vmcore_handlers(crash).postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["log"]
            )
        # The provider raises CategorizedError; the tool returns a typed failure, never a 500.
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_postmortem_triage_runs_and_relabels_actions(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _crashed_with_built_run(pool)
            crash = _FakeCrash()
            resp = await _vmcore_handlers(crash).postmortem_triage(pool, _ctx(), run_id=run_id)
        assert resp.status != "error"
        assert "hunter2" not in resp.data["transcript"]
        assert resp.suggested_next_actions == ["postmortem.triage", "artifacts.list"]
        assert crash.kwargs["commands"] == ["log", "bt"]  # the fixed triage batch

    asyncio.run(_run())


def test_postmortem_triage_propagates_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            resp = await _vmcore_handlers().postmortem_triage(pool, _ctx(), run_id=run_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_postmortem_crash_no_core_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool, sys_id, debuginfo_ref="k/runs/r/vmlinux", build_id="deadbeef"
            )
            resp = await _vmcore_handlers().postmortem_crash(
                pool, _ctx(), run_id=run_id, commands=["log"]
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


# --- surface-wide redaction guard ----------------------------------------------------------


def test_no_raw_vmcore_key_in_any_read_response(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            job = await _enqueue_capture(pool, sys_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(conn, job, _FakeRetriever(sys_id))
            refs: list[str] = []
            from kdive.mcp.tools.catalog import artifacts as artifacts_tools

            vmcores = await vmcore_tools.list_vmcores(pool, _ctx(), system_id=sys_id)
            for r in vmcores.collection_items():
                refs.extend(r.refs.values())
            listed = await artifacts_tools.artifacts_list(pool, _ctx(), system_id=sys_id)
            artifact_items = listed.collection_items()
            for r in artifact_items:
                refs.extend(r.refs.values())
            for r in artifact_items:
                got = await artifacts_tools.artifacts_get(pool, _ctx(), artifact_id=r.object_id)
                refs.extend(got.refs.values())
        assert refs  # something was returned
        # A raw core is `.../vmcore-{method}` (no `-redacted`); it must never surface.
        assert all(not ("/vmcore-" in key and not key.endswith("-redacted")) for key in refs)

    asyncio.run(_run())


# --- registration --------------------------------------------------------------------------


def test_register_handlers_binds_capture_vmcore() -> None:
    registry = HandlerRegistry()
    vmcore_plane.register_handlers(registry, retriever=_FakeRetriever("x"))
    assert registry.get(JobKind.CAPTURE_VMCORE) is not None


def test_register_handlers_requires_provider_runtime_or_retriever() -> None:
    registry = HandlerRegistry()
    with pytest.raises(RuntimeError, match="provider runtime or retriever"):
        vmcore_plane.register_handlers(registry)
