"""The M0 walking-skeleton exit-criterion tests (#26, ADR-0035).

The full happy spine over a real KVM host is now driven by the M1.2 phase-structured spine
driver `tests/integration/test_live_stack.py` (over the live MCP HTTP transport); the M0
`live_vm` full-path stub (`test_walking_skeleton_full_path`) it replaced has been deleted
(ADR-0042 §5). Three of the six M0 exit criteria are decided by **policy over data**, not by
the hypervisor, so they are exercised here as non-gated tests that call handlers directly with
injected fakes — the repo's unit of testing (ADR-0019: handlers, never MCP). They run on every
PR against the disposable Postgres (ADR-0015):

- Exit criterion #6 (destructive gate refusal) — ``test_force_crash_refused_when_gate_check_absent``
- Exit criterion #4 (idempotent step replay) — ``test_completed_step_replay_does_not_re_execute``
- Exit criterion #3 (redaction) — ``test_planted_secret_is_redacted`` + the
  artifact-sensitivity guard
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Job, JobKind
from kdive.domain.state import SystemState
from kdive.jobs import queue
from kdive.jobs.handlers import runs as runs_handlers
from kdive.jobs.handlers import vmcore as vmcore_plane
from kdive.jobs.payloads import Authorizing, BuildPayload, CaptureVmcorePayload
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.catalog.artifacts.reads import artifacts_get, artifacts_list
from kdive.mcp.tools.lifecycle import control as control_tools
from kdive.mcp.tools.lifecycle import vmcore as vmcore_tools
from kdive.provider_components.build_results import BuildOutput
from kdive.providers.ports import CaptureOutput, CrashOutput
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.integration._seed import (
    seed_crashed_system_with_run,
    seed_granted_allocation,
    seed_running_run,
    seed_system,
)
from tests.integration.conftest import open_pool, request_context
from tests.mcp.json_data import data_str
from tests.mcp.systems_support import provider_resolver

_AUTH = Authorizing(principal="user-1", agent_session="sess-1", project="proj")


def _admin_ctx() -> RequestContext:
    return request_context(Role.ADMIN)


# --- fakes (injected providers; the real ops are live_vm-gated) ----------------------------


class _RecordingBuilder:
    """Records build() calls so a replay can assert the rebuild was skipped (#4)."""

    def __init__(self) -> None:
        self.calls: list[UUID] = []

    def build(self, run_id: UUID, profile: object) -> BuildOutput:
        self.calls.append(run_id)
        return BuildOutput(
            kernel_ref=f"proj/runs/{run_id}/kernel",
            debuginfo_ref=f"proj/runs/{run_id}/vmlinux",
            build_id="abcdef0123456789",
        )


class _SecretBearingRetriever:
    """Returns a capture output whose redacted derivative is the response-eligible row (#3)."""

    def __init__(self, system_id: str) -> None:
        self._system_id = system_id
        self.calls = 0

    def capture(self, system_id: UUID, method: CaptureMethod) -> CaptureOutput:
        from kdive.domain.models import Sensitivity
        from kdive.provider_components.artifacts import StoredArtifact

        self.calls += 1
        raw = StoredArtifact(
            f"local/systems/{self._system_id}/vmcore-host_dump",
            "e1",
            Sensitivity.SENSITIVE,
            "vmcore",
        )
        red = StoredArtifact(
            f"local/systems/{self._system_id}/vmcore-host_dump-redacted",
            "e2",
            Sensitivity.REDACTED,
            "vmcore",
        )
        return CaptureOutput(raw=raw, redacted=red, vmcore_build_id="deadbeef")


class _SecretBearingCrash:
    """A CrashPostmortem whose transcript carries a planted secret (#3 transcript redaction)."""

    # Fake credential the test asserts is masked; not a real secret.
    PLANTED_SECRET = "hunter2-s3cr3t"  # pragma: allowlist secret

    def run_crash_postmortem(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str, commands: list[str]
    ) -> CrashOutput:
        return CrashOutput(
            results={c: {"ran": True} for c in commands},
            transcript=f"$ log\npassword={self.PLANTED_SECRET}\nbt\nok",
            truncated=False,
        )


# --- exit criterion #6: destructive gate refusal -------------------------------------------


@pytest.mark.parametrize(
    ("scope_ok", "is_admin", "opt_in", "missing"),
    [
        (False, True, True, "capability_scope"),
        (True, False, True, "admin_role"),
        (True, True, False, "profile_opt_in"),
    ],
)
def test_force_crash_refused_when_gate_check_absent(
    migrated_url: str, scope_ok: bool, is_admin: bool, opt_in: bool, missing: str
) -> None:
    """#6: force_crash is refused (and audited, with no job) when any gate check is absent."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            scope = {"destructive_ops": ["force_crash"]} if scope_ok else {}
            ops = ["force_crash"] if opt_in else []
            alloc_id = await seed_granted_allocation(pool, capability_scope=scope)
            sys_id = await seed_system(
                pool, alloc_id, SystemState.READY, destructive_ops=ops, domain_name="kdive-x"
            )
            ctx = _admin_ctx() if is_admin else request_context(Role.OPERATOR)
            resp = await control_tools.force_crash_system(
                pool, ctx, system_id=sys_id, resolver=provider_resolver()
            )
            assert resp.status == "error"
            assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log "
                    "WHERE object_id = %s AND transition = 'force_crash:denied'",
                    (sys_id,),
                )
                denied = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'force_crash'")
                jobs = await cur.fetchone()
            assert denied is not None and denied["n"] == 1, f"missing check {missing} not audited"
            assert jobs is not None and jobs["n"] == 0  # refusal enqueues no destructive job

    asyncio.run(_run())


def test_force_crash_allowed_when_all_gate_checks_present(migrated_url: str) -> None:
    """The gate's positive control: all three checks present admits a force_crash job."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            alloc_id = await seed_granted_allocation(
                pool, capability_scope={"destructive_ops": ["force_crash"]}
            )
            sys_id = await seed_system(
                pool,
                alloc_id,
                SystemState.READY,
                destructive_ops=["force_crash"],
                domain_name="kdive-x",
            )
            resp = await control_tools.force_crash_system(
                pool, _admin_ctx(), system_id=sys_id, resolver=provider_resolver()
            )
            assert resp.status == "queued"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s",
                    (f"{sys_id}:force_crash",),
                )
                row = await cur.fetchone()
            assert row is not None and row["n"] == 1

    asyncio.run(_run())


# --- exit criterion #4: idempotent step replay ---------------------------------------------


async def _enqueue_build(pool: AsyncConnectionPool, run_id: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn, JobKind.BUILD, BuildPayload(run_id=run_id), _AUTH, f"{run_id}:build"
        )


def test_completed_step_replay_does_not_re_execute(migrated_url: str) -> None:
    """#4: re-dispatching a completed build job reads the ledger and does not rebuild."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            alloc_id = await seed_granted_allocation(pool)
            sys_id = await seed_system(pool, alloc_id, SystemState.READY)
            run_id = await seed_running_run(pool, sys_id)
            job = await _enqueue_build(pool, run_id)
            builder = _RecordingBuilder()
            async with pool.connection() as conn:
                await runs_handlers.build_handler(conn, job, builder)
            # Replay the same job: the (run_id, "build") ledger short-circuits the rebuild.
            async with pool.connection() as conn:
                await runs_handlers.build_handler(conn, job, builder)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id = %s", (run_id,))
                run_row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM run_steps WHERE run_id = %s AND step = 'build'",
                    (run_id,),
                )
                ledger = await cur.fetchone()
            assert builder.calls == [UUID(run_id)]  # built exactly once across the replay
            assert run_row is not None and run_row["state"] == "succeeded"
            assert ledger is not None and ledger["n"] == 1  # one ledger row, not two

    asyncio.run(_run())


# --- exit criterion #3: redaction ----------------------------------------------------------


async def _enqueue_capture(
    pool: AsyncConnectionPool, system_id: str, method: str = "host_dump"
) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.CAPTURE_VMCORE,
            CaptureVmcorePayload(system_id=system_id, method=CaptureMethod(method)),
            _AUTH,
            f"{system_id}:capture_vmcore:{method}",
        )


def test_planted_secret_is_redacted(migrated_url: str) -> None:
    """#3(a): a planted secret in a transcript is masked in the returned postmortem envelope."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            sys_id, run_id = await seed_crashed_system_with_run(pool)
            job = await _enqueue_capture(pool, sys_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(conn, job, _SecretBearingRetriever(sys_id))
            secret_registry = SecretRegistry()
            secret_registry.register(_SecretBearingCrash.PLANTED_SECRET, scope="test")
            handlers = vmcore_tools.VmcoreHandlers(
                supported_methods=frozenset({CaptureMethod.HOST_DUMP}),
                crash=_SecretBearingCrash(),
                secret_registry=secret_registry,
            )
            resp = await handlers.postmortem_crash(
                pool,
                request_context(),
                run_id=run_id,
                commands=["log"],
            )
            assert resp.status != "error"
            transcript = data_str(resp, "transcript")
            assert _SecretBearingCrash.PLANTED_SECRET not in transcript
            assert "[REDACTED]" in transcript

    asyncio.run(_run())


def test_raw_vmcore_is_sensitive_and_unreachable(migrated_url: str) -> None:
    """#3(b): only the redacted artifact is response-eligible; the raw vmcore key never leaks."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            sys_id, _ = await seed_crashed_system_with_run(pool)
            job = await _enqueue_capture(pool, sys_id)
            async with pool.connection() as conn:
                await vmcore_plane.capture_handler(conn, job, _SecretBearingRetriever(sys_id))
            ctx = request_context()
            refs: list[str] = []
            vmcores = await vmcore_tools.list_vmcores(pool, ctx, system_id=sys_id)
            for r in vmcores.items:
                refs.extend(r.refs.values())
            listed = await artifacts_list(pool, ctx, system_id=sys_id)
            for r in listed.items:
                refs.extend(r.refs.values())
                got = await artifacts_get(pool, ctx, artifact_id=r.object_id)
                refs.extend(got.refs.values())
            # The raw `sensitive` row's id is known only via direct SQL; artifacts.get on it is
            # not-found-shaped (no leak even by id).
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT id FROM artifacts WHERE owner_id = %s AND sensitivity = 'sensitive'",
                    (sys_id,),
                )
                raw_row = await cur.fetchone()
            assert raw_row is not None
            raw_get = await artifacts_get(pool, ctx, artifact_id=str(raw_row["id"]))
            assert raw_get.status == "error"  # the raw row is unfetchable through the surface
        assert refs  # the redacted artifact was returned
        # A raw core is `.../vmcore-{method}` (no `-redacted`); it must never surface.
        assert all(not ("/vmcore-" in key and not key.endswith("-redacted")) for key in refs)

    asyncio.run(_run())
