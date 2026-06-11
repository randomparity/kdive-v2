"""Shared, provider-agnostic live-stack spine scaffolding (ADR-0042 §4/§5, ADR-0045/0046).

The local-libvirt spine (``test_live_stack.py``) and the remote-libvirt spine
(``test_remote_live_stack.py``) drive the same shape — allocate → … → release → teardown →
report — over the live MCP HTTP transport. The contract they share lives here so a fix to the
phase-naming, drain/state-polling, out-of-band DB seeding, or accounting-report assertions lands
in one place: the phase-naming contract (``phase`` / ``SpinePhaseError``), the envelope helpers
(``ok`` / ``scalar``), the async-drain helpers (``drain_job`` / ``await_system_state`` — both with
an overridable deadline so a longer phase can extend its budget), the per-project role-token
factory (``mint_role_token``), the out-of-band metering/capability seeders, and the audit / ledger
/ report helpers. Provider-specific pieces (profile factories, the booted-spine bodies, the
owned-infra teardown check) stay in each spine's own module.
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

from kdive.domain.cost import quantize_kcu
from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.harness import LiveStackClient, OidcIssuer, mint_token

# Above the 300s jobs.wait cap and the 30s reconciler interval; teardown is the slowest phase.
DRAIN_DEADLINE_S = 600.0
POLL_INTERVAL_S = 2.0

# The remote allocation's disk request and the provision profile's disk_gb must agree, or
# reconcile_profile_sizing rejects the mismatch before provision runs (#315). One source.
REMOTE_ALLOCATION_DISK_GB = 10

_ARTIFACT_DIR_ENV = "KDIVE_ARTIFACT_DIR"


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


# --- envelope-assert helpers ----------------------------------------------------------------


def ok(envelope: ToolResponse, phase_name: str) -> ToolResponse:
    """Return the envelope if non-failure, else raise a SpinePhaseError naming the phase."""
    if envelope.status in {"error", "failed"}:
        raise SpinePhaseError(
            phase_name, f"{envelope.status} envelope", error_category=envelope.error_category
        )
    return envelope


async def scalar(client: LiveStackClient, name: str, **args: object) -> ToolResponse:
    """Call a scalar tool and narrow the result to a single ``ToolResponse``."""
    env = await client.call_tool(name, **args)
    assert isinstance(env, ToolResponse), f"{name} returned a list, expected one envelope"
    return env


# --- async-drain helpers (ADR-0045 §2) ------------------------------------------------------


async def drain_job(
    client: LiveStackClient,
    phase_name: str,
    job_id: str,
    *,
    deadline_s: float = DRAIN_DEADLINE_S,
) -> ToolResponse:
    """Poll jobs.wait until the job succeeds; classify the three outcomes (ADR-0045 §2).

    ``deadline_s`` is overridable so a longer phase (the remote two-phase capture, which waits
    out a server-side readiness window) can extend the drain budget.
    """
    deadline = time.monotonic() + deadline_s
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
        await asyncio.sleep(POLL_INTERVAL_S)


async def await_system_state(
    client: LiveStackClient,
    phase_name: str,
    system_id: str,
    target: str,
    *,
    deadline_s: float = DRAIN_DEADLINE_S,
) -> None:
    """Poll systems.get until the System reaches ``target`` state (or the deadline)."""
    deadline = time.monotonic() + deadline_s
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
        await asyncio.sleep(POLL_INTERVAL_S)


# --- per-role token factory -----------------------------------------------------------------


def mint_role_token(
    issuer: OidcIssuer,
    *,
    project: str,
    agent_session: str,
    role: str,
    platform_roles: list[str] | None = None,
) -> str:
    """Mint a per-project role token (the local test's ``_token``, parameterized by project)."""
    return mint_token(
        issuer,
        subject=f"{role}-{project}",
        projects=[project],
        roles={project: role},
        platform_roles=platform_roles,
        agent_session=agent_session,
    )


# --- out-of-band capability grant + metering seed (ADR-0045 §1, ADR-0046 §0) ----------------


async def grant_force_crash_scope(db_url: str, allocation_id: str) -> None:
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


async def seed_metering(
    db_url: str,
    project: str,
    *,
    limit_kcu: str = "1000000",
    max_allocations: int = 4,
    max_systems: int = 4,
) -> None:
    """Seed the budget (limit-only) + quota rows admission requires, out of band.

    The budget upsert writes ``limit_kcu`` only and leaves ``spent_kcu`` untouched (matching
    production ``set_budget`` / ``BUDGETS.upsert``), so a re-run of the fixed-constant project
    keeps the DB-maintained running total consistent with the ledger Σ; a first insert starts it
    at 0. Both upserts are idempotent on the ``project`` primary key.
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.execute(
            "INSERT INTO budgets (project, limit_kcu) VALUES (%s, %s) "
            "ON CONFLICT (project) DO UPDATE SET limit_kcu = EXCLUDED.limit_kcu",
            (project, limit_kcu),
        )
        await conn.execute(
            "INSERT INTO quotas (project, max_concurrent_allocations, max_concurrent_systems) "
            "VALUES (%s, %s, %s) ON CONFLICT (project) DO UPDATE SET "
            "max_concurrent_allocations = EXCLUDED.max_concurrent_allocations, "
            "max_concurrent_systems = EXCLUDED.max_concurrent_systems",
            (project, max_allocations, max_systems),
        )
        await conn.commit()


# --- allocate / provision / crash phase helpers ---------------------------------------------


async def allocate_remote(
    client: LiveStackClient,
    db_url: str,
    *,
    project: str,
    phase_name: str,
) -> str:
    """Request a remote-libvirt allocation and grant the force_crash scope; return its id.

    Folds the wire ``allocations.request`` and the out-of-band capability grant the destructive
    crash needs into one step a multi-System exercise can call once per System.
    """
    env = ok(
        await scalar(
            client,
            "allocations.request",
            project=project,
            request={
                "vcpus": 2,
                "memory_gb": 2,
                "disk_gb": REMOTE_ALLOCATION_DISK_GB,
                "resource": {"mode": "kind", "kind": "remote-libvirt"},
            },
        ),
        phase_name,
    )
    allocation_id = env.object_id
    await grant_force_crash_scope(db_url, allocation_id)
    return allocation_id


async def provision_to_ready(
    client: LiveStackClient,
    *,
    allocation_id: str,
    profile: dict[str, object],
    phase_name: str,
) -> str:
    """Provision a System from an allocation and wait for it to reach ``ready``; return its id."""
    env = ok(
        await scalar(
            client,
            "systems.provision",
            allocation_id=allocation_id,
            profile=profile,
        ),
        phase_name,
    )
    system_id = env.data["system_id"]  # in data, NOT object_id (the job id)
    await await_system_state(client, phase_name, system_id, "ready")
    return system_id


async def crash_to_crashed(
    admin: LiveStackClient,
    *,
    system_id: str,
    phase_name: str,
) -> None:
    """Force-crash a System (admin scope) and wait for it to reach ``crashed``."""
    ok(await scalar(admin, "control.force_crash", system_id=system_id), phase_name)
    await await_system_state(admin, phase_name, system_id, "crashed")


# --- report-phase DB + artifact helpers (ADR-0046 §0/§2/§3) ---------------------------------


async def db_now(db_url: str) -> datetime:
    """Read the Postgres server clock, so the report window shares one clock with ledger.ts."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute("SELECT now()")
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("SELECT now() returned no row")
    return row[0]


async def ledger_sums(db_url: str, project: str, since: datetime) -> tuple[Decimal, Decimal]:
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


async def count_audit(db_url: str, *, object_id: str, transition: str, principal: str) -> int:
    """Count audit_log rows for a transition on an object under a given principal."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM audit_log "
            "WHERE object_id = %s AND transition = %s AND principal = %s",
            (object_id, transition, principal),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def count_audit_suffix(db_url: str, *, object_id: str, suffix: str, principal: str) -> int:
    """Count audit_log rows whose transition ends with ``suffix`` (robust to the prior state).

    The teardown handler writes ``f"{old.value}->torn_down"``; the prior state depends on the
    spine, so match the ``->torn_down`` suffix rather than a fixed literal.
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM audit_log "
            "WHERE object_id = %s AND transition LIKE %s AND principal = %s",
            (object_id, f"%{suffix}", principal),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def system_torn_down(db_url: str, system_id: str) -> bool:
    """True iff the System row is ``torn_down`` (the DB half of the #5 teardown check)."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    return row is not None and row[0] == "torn_down"


def report_artifact_dir() -> Path:
    """Resolve the artifact dir: ``KDIVE_ARTIFACT_DIR`` or an out-of-tree temp default.

    The default lives under ``tempfile.gettempdir()`` (never inside the repo) so a live run does
    not dirty the working tree or get walked by whole-tree tooling (ADR-0046 §3).
    """
    override = os.environ.get(_ARTIFACT_DIR_ENV)
    base = (
        Path(override) if override else Path(tempfile.gettempdir()) / "kdive-live-stack-artifacts"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def write_report_artifact(payload: dict[str, object], *, name: str) -> Path:
    """Write the report payload as ``name`` under the artifact dir; return its path."""
    path = report_artifact_dir() / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def find_project_row(rows: list[dict[str, str]], project: str) -> dict[str, str]:
    """Return the rollup row for ``project``, or fail the phase if absent (no spend rolled up)."""
    for row in rows:
        if row.get("project") == project:
            return row
    raise AssertionError(f"no rollup row for project {project!r} (no spend in the window?)")


async def assert_audit(db_url: str, *, project: str, allocation_id: str, system_id: str) -> None:
    """#2: audit per transition + force_crash, split by attributing principal."""
    assert (
        await count_audit(
            db_url, object_id=system_id, transition="ready->crashed", principal=f"admin-{project}"
        )
        == 1
    ), "force_crash not audited under admin (#2)"
    assert (
        await count_audit(
            db_url,
            object_id=allocation_id,
            transition="releasing->released",
            principal=f"operator-{project}",
        )
        >= 1
    ), "release not audited under operator (#2)"
    # teardown is enqueued + audited by the reconciler (ADR-0021), under system:reconciler, NOT
    # the driver. The handler writes f"{old.value}->torn_down"; the spine crashed first, so the
    # row is crashed->torn_down — match the suffix to stay robust to the prior state.
    assert (
        await count_audit_suffix(
            db_url, object_id=system_id, suffix="->torn_down", principal="system:reconciler"
        )
        >= 1
    ), "teardown not audited under system:reconciler (#2)"


async def assert_report(
    base_url: str,
    auditor_token: str,
    db_url: str,
    window_start: datetime,
    *,
    project: str,
    artifact_name: str,
) -> None:
    """Drive accounting.report_all_projects under platform_auditor; assert windowed spend.

    Asserts the ``project`` rollup row reflects this run's real spend (windowed wire rollup ==
    windowed DB ledger sums), then emits + re-asserts the JSON report artifact (ADR-0046 §2/§3).
    """
    auditor = LiveStackClient.over_http(base_url, auditor_token)
    async with auditor:
        env = ok(
            await scalar(
                auditor,
                "accounting.report_all_projects",
                window=[window_start.isoformat(), None],
            ),
            "report",
        )
    rows = [item.data for item in env.items]
    total = {
        "reserved": env.data["total_reserved"],
        "reconciled": env.data["total_reconciled"],
        "variance": env.data["total_variance"],
    }
    row = find_project_row(rows, project)
    reserved = Decimal(str(row["reserved"]))
    reconciled = Decimal(str(row["reconciled"]))
    variance = Decimal(str(row["variance"]))
    assert reserved > 0, "report shows no reserved spend for the run (#101)"
    assert variance == reconciled - reserved, "report variance != reconciled - reserved (#101)"
    db_reserved, db_reconciled = await ledger_sums(db_url, project, window_start)
    assert reserved == db_reserved, f"wire reserved {reserved} != DB {db_reserved} (#101)"
    assert reconciled == db_reconciled, f"wire reconciled {reconciled} != DB {db_reconciled} (#101)"
    artifact = write_report_artifact(
        {
            "scope": env.data["scope"],
            "window": [window_start.isoformat(), None],
            "project_row": row,
            "total": total,
        },
        name=artifact_name,
    )
    assert artifact.exists(), f"report artifact not written at {artifact} (#101)"
    written = json.loads(artifact.read_text())
    project_row = written["project_row"]
    assert Decimal(str(project_row["reserved"])) == reserved, "artifact reserved drift (#101)"
    assert Decimal(str(project_row["reconciled"])) == reconciled, "artifact reconciled drift (#101)"
    assert Decimal(str(project_row["variance"])) == variance, "artifact variance drift (#101)"
