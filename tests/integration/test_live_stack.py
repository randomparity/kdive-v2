"""The phase-structured live-stack spine driver (#100, ADR-0042 §1/§4/§5, ADR-0045).

Drives the full kdive spine — allocate → provision → open-investigation → create-run → build →
install → boot → attach → crash → capture → introspect → release → (reconciler) teardown — over
the **live MCP HTTP transport** via the merged harness (``mint_token`` + ``LiveStackClient``),
each step a tool call under a specific OIDC role token, the async job kinds drained by the real
host ``worker`` + ``reconciler``. Every step records pass/fail; a failure **names its phase**
(``SpinePhaseError``). The suite is ``live_stack``-marked and preflights to a clean skip unless
the VM fixtures + a reachable stack + the issuer + ``KDIVE_DATABASE_URL`` are all present, so it
is safe in CI and on any host (CI deselects ``live_stack``).

Acceptance asserted over the wire / against the stack's Postgres + MinIO: protocol (well-formed
envelopes, JWKS-validated tokens), #1 (redacted vmcore in MinIO), #2 (audit per transition +
force_crash, split by attributing principal — driver vs ``system:reconciler``), #3 (redaction
does not leak through the wire), #5 (``torn_down`` + ``Discovery.list_owned()`` empty), the
report phase (``accounting.report`` all-projects form under a ``platform_auditor`` token,
windowed to this run, asserting ``reserved``/``reconciled``/variance against the ledger and
emitting a JSON report artifact — ADR-0046), and the RBAC negatives (viewer raised-path;
operator force_crash ``authorization_denied`` envelope; project-only token denied the
all-projects report with an ``authorization_denied`` envelope).

Two non-gated unit tests exercise the ``phase`` naming contract so a regression is caught in
normal CI; the spine + RBAC tests are ``live_stack``-marked and skip without a stack.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from kdive.domain.cost import quantize_kcu
from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.harness import (
    LiveStackClient,
    LiveStackToolError,
    OidcIssuer,
    mint_token,
)

_GUEST_IMAGE_ENV = "KDIVE_GUEST_IMAGE"
_KERNEL_TREE_ENV = "KDIVE_KERNEL_SRC"
_DATABASE_URL_ENV = "KDIVE_DATABASE_URL"
_PROJECT = "spine-proj"
_AGENT_SESSION = "spine-sess"
# Above the 300s jobs.wait cap and the 30s reconciler interval; teardown is the slowest phase.
_DRAIN_DEADLINE_S = 600.0
_POLL_INTERVAL_S = 2.0


# --- phase-failure naming contract (ADR-0042 §4, ADR-0045 §2) -------------------------------


class SpinePhaseError(AssertionError):
    """A spine phase failed; carries the phase name so a failure says which step died."""

    def __init__(self, phase: str, reason: str, *, error_category: str | None = None) -> None:
        self.phase = phase
        self.reason = reason
        self.error_category = error_category
        super().__init__(f"phase {phase!r} failed: {reason}")


@asynccontextmanager
async def phase(name: str) -> AsyncIterator[None]:
    """Run a phase; convert any failure into a ``SpinePhaseError`` naming the phase."""
    try:
        yield
    except SpinePhaseError:
        raise  # an inner phase already named itself; do not re-wrap
    except Exception as exc:  # noqa: BLE001 — deliberately broad: every failure names its phase
        raise SpinePhaseError(name, str(exc)) from exc


def test_phase_names_the_failing_phase() -> None:
    """A raised exception inside a phase becomes a SpinePhaseError naming that phase."""

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            async with phase("provision"):
                raise ValueError("libvirt exploded")
        assert excinfo.value.phase == "provision"
        assert isinstance(excinfo.value.__cause__, ValueError)

    asyncio.run(_run())


def test_phase_passes_through_spine_phase_error() -> None:
    """An inner SpinePhaseError is preserved (not re-wrapped under the outer phase name)."""

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            async with phase("outer"):
                raise SpinePhaseError("boot", "job failed", error_category="infrastructure_failure")
        assert excinfo.value.phase == "boot"

    asyncio.run(_run())


# --- preflight + envelope-assert helper -----------------------------------------------------


def _spine_preflight() -> tuple[OidcIssuer, str, str]:
    """Resolve issuer + stack URL + DB URL, or skip with the exact fix (ADR-0035 §4)."""
    image = os.environ.get(_GUEST_IMAGE_ENV)
    if not image or not Path(image).exists():
        pytest.skip(
            f"{_GUEST_IMAGE_ENV} unset or missing; run scripts/live-vm/build-guest-image.sh"
        )
    tree = os.environ.get(_KERNEL_TREE_ENV)
    if not tree or not Path(tree).exists():
        pytest.skip(
            f"{_KERNEL_TREE_ENV} unset or missing; run scripts/live-vm/fetch-kernel-tree.sh"
        )
    db_url = os.environ.get(_DATABASE_URL_ENV)
    if not db_url:
        pytest.skip(f"{_DATABASE_URL_ENV} unset; bring up the stack (see the live-stack runbook)")
    issuer = require_issuer()  # skips if the OIDC issuer is unset/unreachable
    base_url = require_stack()  # skips if KDIVE_STACK_BASE_URL is unset
    return issuer, base_url, db_url


def _wire_preflight() -> tuple[OidcIssuer, str]:
    """Resolve issuer + stack URL for the RBAC-negative wire checks (no VM, no DB).

    These tests exercise a denial that fires in the auth/RBAC layer before any provisioning
    or DB read, so they need only a reachable issuer and a running server — not the guest
    image / kernel tree the booting spine requires. Gating them behind ``_spine_preflight``
    would make them skip forever on any host without the (currently unbuildable) VM fixtures.
    """
    issuer = require_issuer()  # skips if the OIDC issuer is unset/unreachable
    base_url = require_stack()  # skips if KDIVE_STACK_BASE_URL is unset
    return issuer, base_url


def _ok(envelope: ToolResponse, phase_name: str) -> ToolResponse:
    """Return the envelope if non-failure, else raise a SpinePhaseError naming the phase."""
    if envelope.status in {"error", "failed"}:
        raise SpinePhaseError(
            phase_name, f"{envelope.status} envelope", error_category=envelope.error_category
        )
    return envelope


# --- out-of-band capability grant + audit/teardown DB helpers (ADR-0045 §1) -----------------


async def _grant_force_crash_scope(db_url: str, allocation_id: str) -> None:
    """Grant the destructive capability scope on an allocation, out of band (ADR-0045 §1).

    The wire ``allocations.request`` always grants an empty scope; granting a destructive
    capability is a privileged platform action no operator tool exposes. This mirrors
    ``seed_granted_allocation(capability_scope=…)`` — the platform-admin action stood in for.
    """
    scope = '{"destructive_ops": ["force_crash"]}'
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.execute(
            "UPDATE allocations SET capability_scope = %s::jsonb WHERE id = %s",
            (scope, allocation_id),
        )
        await conn.commit()


# --- out-of-band metering seed + report-phase DB helpers (ADR-0046 §0/§2) --------------------

# Admission is fail-closed on metering (ADR-0007 §4): _within_budget and _within_alloc_quota
# both deny a project with no row, writing no ledger row. The spine never sets a budget over
# the wire, so seed both out of band before allocate (mirrors _grant_force_crash_scope), or the
# report phase has no spend to assert (ADR-0046 §0).
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


_ARTIFACT_DIR_ENV = "KDIVE_ARTIFACT_DIR"
_ARTIFACT_NAME = "accounting-report.json"


def _report_artifact_dir() -> Path:
    """Resolve the artifact dir: ``KDIVE_ARTIFACT_DIR`` or an out-of-tree temp default.

    The default lives under ``tempfile.gettempdir()`` (never inside the repo) so a live run
    does not dirty the working tree or get walked by whole-tree tooling (ADR-0046 §3).
    """
    override = os.environ.get(_ARTIFACT_DIR_ENV)
    base = (
        Path(override) if override else Path(tempfile.gettempdir()) / "kdive-live-stack-artifacts"
    )
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


async def _count_audit(db_url: str, *, object_id: str, transition: str, principal: str) -> int:
    """Count audit_log rows for a transition on an object under a given principal."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM audit_log "
            "WHERE object_id = %s AND transition = %s AND principal = %s",
            (object_id, transition, principal),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _count_audit_suffix(db_url: str, *, object_id: str, suffix: str, principal: str) -> int:
    """Count audit_log rows whose transition ends with ``suffix`` (robust to the prior state).

    The teardown handler writes ``f"{old.value}->torn_down"``; the prior state depends on the
    spine (``crashed`` here), so match the ``->torn_down`` suffix rather than a fixed literal.
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM audit_log "
            "WHERE object_id = %s AND transition LIKE %s AND principal = %s",
            (object_id, f"%{suffix}", principal),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _system_torn_down(db_url: str, system_id: str) -> bool:
    """True iff the System row is ``torn_down`` (the DB half of the #5 teardown check)."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    return row is not None and row[0] == "torn_down"


# --- async-drain helpers (ADR-0045 §2) ------------------------------------------------------


async def _drain_job(client: LiveStackClient, phase_name: str, job_id: str) -> ToolResponse:
    """Poll jobs.wait until the job is succeeded; classify the three outcomes (ADR-0045 §2)."""
    deadline = time.monotonic() + _DRAIN_DEADLINE_S
    while True:
        env = await client.call_tool("jobs.wait", job_id=job_id, timeout_s=60.0)
        assert isinstance(env, ToolResponse)
        if env.status == "succeeded":
            return env
        if env.status in {"failed", "canceled"}:
            raise SpinePhaseError(
                phase_name, f"job {env.status}", error_category=env.error_category
            )
        if time.monotonic() >= deadline:  # non-terminal return: a worker stall
            raise SpinePhaseError(phase_name, "drain_timeout")
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _await_system_state(
    client: LiveStackClient, phase_name: str, system_id: str, target: str
) -> None:
    """Poll systems.get until the System reaches ``target`` state (or the deadline)."""
    deadline = time.monotonic() + _DRAIN_DEADLINE_S
    while True:
        env = await client.call_tool("systems.get", system_id=system_id)
        assert isinstance(env, ToolResponse)
        if env.status == target:
            return
        if env.status in {"error", "failed"}:
            raise SpinePhaseError(
                phase_name, f"system {env.status}", error_category=env.error_category
            )
        if time.monotonic() >= deadline:
            raise SpinePhaseError(phase_name, f"system did not reach {target}")
        await asyncio.sleep(_POLL_INTERVAL_S)


# --- per-role tokens/clients + profiles -----------------------------------------------------


def _token(issuer: OidcIssuer, *, role: str, platform_roles: list[str] | None = None) -> str:
    return mint_token(
        issuer,
        subject=f"{role}-{_PROJECT}",
        projects=[_PROJECT],
        roles={_PROJECT: role},
        platform_roles=platform_roles,
        agent_session=_AGENT_SESSION,
    )


def _provision_profile() -> dict[str, object]:
    """A provisioning profile that opts force_crash in (the gate's profile factor, ADR-0045)."""
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "provider": {
            "local-libvirt": {
                "rootfs_image_ref": os.environ[_GUEST_IMAGE_ENV],
                "crashkernel": "256M",
                "destructive_ops": ["force_crash"],
            }
        },
    }


def _build_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "config_ref": "file:///configs/kdump.config",
    }


# --- RBAC negative: the raised path (no real system needed) ----------------------------------


@pytest.mark.live_stack
def test_viewer_denied_operator_op_over_the_wire() -> None:
    """A viewer token is denied an operator op; require_role raises → a tool error over HTTP.

    The viewer token carries the spine project (role ``viewer``), so the denial exercises the
    ``require_role`` (role) boundary, not the ``require_project`` (membership) boundary that
    ``allocations.request`` checks first.
    """
    issuer, base_url = _wire_preflight()

    async def _run() -> None:
        viewer = LiveStackClient.over_http(base_url, _token(issuer, role="viewer"))
        async with viewer:
            with pytest.raises(LiveStackToolError):  # require_role raises → tool error
                await viewer.call_tool(
                    "allocations.request", project=_PROJECT, vcpus=1, memory_gb=1
                )

    asyncio.run(_run())


@pytest.mark.live_stack
def test_report_all_projects_denied_to_project_token() -> None:
    """A project-only token is denied accounting.report's all-projects form over the wire.

    Verified against the tool: the all-projects form catches the raised AuthorizationError and
    *returns* ToolResponse.failure(..., AUTHORIZATION_DENIED) — a well-formed error envelope,
    not a raised tool error. So assert the envelope shape (like crash-rbac-negative), not a
    raised LiveStackToolError (ADR-0046 §3).
    """
    issuer, base_url = _wire_preflight()

    async def _run() -> None:
        project_only = LiveStackClient.over_http(base_url, _token(issuer, role="viewer"))
        async with project_only:
            denied = await _scalar(project_only, "accounting.report", scope="all-projects")
        assert denied.status == "error", "project-only token was not denied (#101)"
        assert denied.error_category == "authorization_denied", "wrong denial category (#101)"

    asyncio.run(_run())


# --- the full spine -------------------------------------------------------------------------


@pytest.mark.live_stack
def test_spine_over_the_wire() -> None:
    """Drive allocate → … → teardown over HTTP; assert #1/#2/#3/#5; name the failing phase."""
    issuer, base_url, db_url = _spine_preflight()
    operator_token = _token(issuer, role="operator")
    admin_token = _token(issuer, role="admin")
    auditor_token = _token(issuer, role="viewer", platform_roles=["platform_auditor"])

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, operator_token)
        admin = LiveStackClient.over_http(base_url, admin_token)
        system_id = allocation_id = run_id = ""
        async with op, admin:
            # out-of-band: meter the project (admission is fail-closed, ADR-0046 §0), then
            # capture the report window start from the DB clock (shares ledger.ts's clock).
            await _seed_metering(db_url, _PROJECT)
            window_start = await _db_now(db_url)
            async with phase("allocate"):
                env = _ok(
                    await _scalar(
                        op, "allocations.request", project=_PROJECT, vcpus=2, memory_gb=2
                    ),
                    "allocate",
                )
                allocation_id = env.object_id
            # out-of-band: grant the destructive capability scope (ADR-0045 §1)
            await _grant_force_crash_scope(db_url, allocation_id)
            async with phase("provision"):
                env = _ok(
                    await _scalar(
                        op,
                        "systems.provision",
                        allocation_id=allocation_id,
                        profile=_provision_profile(),
                    ),
                    "provision",
                )
                system_id = env.data["system_id"]  # in data, NOT object_id (the job id)
                await _await_system_state(op, "provision", system_id, "ready")
            async with phase("open-investigation"):
                env = _ok(
                    await _scalar(op, "investigations.open", project=_PROJECT, title="spine"),
                    "open-investigation",
                )
                investigation_id = env.object_id
            async with phase("create-run"):
                env = _ok(
                    await _scalar(
                        op,
                        "runs.create",
                        investigation_id=investigation_id,
                        system_id=system_id,
                        build_profile=_build_profile(),
                    ),
                    "create-run",
                )
                run_id = env.object_id
            for step in ("build", "install", "boot"):
                async with phase(step):
                    env = _ok(await _scalar(op, f"runs.{step}", run_id=run_id), step)
                    await _drain_job(op, step, env.object_id)
            async with phase("attach"):
                env = _ok(
                    await _scalar(op, "debug.start_session", run_id=run_id, transport="gdbstub"),
                    "attach",
                )
                session_id = env.object_id
                _ok(
                    await _scalar(
                        op, "debug.read_registers", session_id=session_id, registers=["rip"]
                    ),
                    "attach",
                )
            async with phase("crash-rbac-negative"):
                denied = await _scalar(op, "control.force_crash", system_id=system_id)
                if denied.status != "error" or denied.error_category != "authorization_denied":
                    raise SpinePhaseError("crash-rbac-negative", "operator was not denied")
            async with phase("crash"):
                _ok(await _scalar(admin, "control.force_crash", system_id=system_id), "crash")
                await _await_system_state(admin, "crash", system_id, "crashed")
            async with phase("capture"):
                env = _ok(await _scalar(op, "vmcore.fetch", system_id=system_id), "capture")
                await _drain_job(op, "capture", env.object_id)
                cores = await op.call_tool("vmcore.list", system_id=system_id)
                assert isinstance(cores, list) and cores, "no vmcore artifact listed (#1)"
                refs = [v for c in cores for v in c.refs.values()]
                assert refs, "no vmcore refs (#1)"
                assert all(not r.endswith("/vmcore") for r in refs), "raw vmcore leaked (#1)"
            async with phase("introspect"):
                env = _ok(await _scalar(op, "introspect.from_vmcore", run_id=run_id), "introspect")
                report = env.data.get("report", "")
                assert "hunter2" not in report and "password=" not in report, "secret leaked (#3)"
            async with phase("release"):
                _ok(
                    await _scalar(op, "allocations.release", allocation_id=allocation_id),
                    "release",
                )
            async with phase("teardown"):  # reconciler-driven (≥30s) → torn_down
                await _await_system_state(op, "teardown", system_id, "torn_down")
            async with phase("report"):  # all-projects rollup under platform_auditor
                await _assert_report(base_url, auditor_token, db_url, window_start)

        await _assert_audit(db_url, allocation_id=allocation_id, system_id=system_id)
        await _assert_teardown(db_url, system_id)

    asyncio.run(_run())


async def _scalar(client: LiveStackClient, name: str, **args: object) -> ToolResponse:
    """Call a scalar tool and narrow the result to a single ``ToolResponse``."""
    env = await client.call_tool(name, **args)
    assert isinstance(env, ToolResponse), f"{name} returned a list, expected one envelope"
    return env


async def _assert_audit(db_url: str, *, allocation_id: str, system_id: str) -> None:
    """#2: audit per transition + force_crash, split by attributing principal."""
    assert (
        await _count_audit(
            db_url, object_id=system_id, transition="ready->crashed", principal=f"admin-{_PROJECT}"
        )
        == 1
    ), "force_crash not audited under admin (#2)"
    assert (
        await _count_audit(
            db_url,
            object_id=allocation_id,
            transition="releasing->released",
            principal=f"operator-{_PROJECT}",
        )
        >= 1
    ), "release not audited under operator (#2)"
    # teardown is enqueued + audited by the reconciler (ADR-0021), under system:reconciler, NOT
    # the driver. The handler writes f"{old.value}->torn_down"; the spine crashed first, so the
    # row is crashed->torn_down — match the suffix to stay robust to the prior state.
    assert (
        await _count_audit_suffix(
            db_url, object_id=system_id, suffix="->torn_down", principal="system:reconciler"
        )
        >= 1
    ), "teardown not audited under system:reconciler (#2)"


async def _assert_teardown(db_url: str, system_id: str) -> None:
    """#5: after teardown the System is torn_down and no OwnedInfra remains."""
    assert await _system_torn_down(db_url, system_id), "system not torn_down (#5)"
    import libvirt  # noqa: PLC0415 — only importable on a libvirt host (the live_stack path)

    from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery  # noqa: PLC0415

    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: libvirt.open("qemu:///system"),  # ty: ignore[invalid-argument-type]
        concurrent_allocation_cap=2,
    )
    owned_ids = {o["system_id"] for o in disc.list_owned()}
    assert system_id not in owned_ids, "released system still owned (#5)"


async def _assert_report(
    base_url: str, auditor_token: str, db_url: str, window_start: datetime
) -> None:
    """Drive accounting.report (all-projects) under platform_auditor; assert windowed spend.

    Asserts the _PROJECT rollup row reflects this run's real spend (windowed wire rollup ==
    windowed DB ledger sums), then emits + re-asserts the JSON report artifact (ADR-0046 §2/§3).
    """
    auditor = LiveStackClient.over_http(base_url, auditor_token)
    async with auditor:
        env = _ok(
            await _scalar(
                auditor,
                "accounting.report",
                scope="all-projects",
                window=[window_start.isoformat(), None],
            ),
            "report",
        )
    rows = json.loads(env.data["rows"])
    total = json.loads(env.data["total"])
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
        {
            "scope": env.data["scope"],
            "window": [window_start.isoformat(), None],
            "project_row": row,
            "total": total,
        }
    )
    assert artifact.exists(), f"report artifact not written at {artifact} (#101)"
    written = json.loads(artifact.read_text())
    project_row = written["project_row"]
    assert Decimal(str(project_row["reserved"])) == reserved, "artifact reserved drift (#101)"
    assert Decimal(str(project_row["reconciled"])) == reconciled, "artifact reconciled drift (#101)"
    assert Decimal(str(project_row["variance"])) == variance, "artifact variance drift (#101)"
