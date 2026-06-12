"""The phase-structured local-libvirt live-stack spine driver (#100, ADR-0042 §1/§4/§5, ADR-0045).

Drives the full kdive spine — allocate → provision → open-investigation → create-run → build →
install → boot → attach → crash → capture → introspect → release → (reconciler) teardown → report
— over the **live MCP HTTP transport** via the merged harness (``mint_token`` + ``LiveStackClient``)
— each step a tool call under a specific OIDC role token, the async job kinds drained by the real
host ``worker`` + ``reconciler``. The provider-agnostic spine scaffolding (phase naming, drain /
state polling, role tokens, out-of-band DB seeding, audit / ledger / report helpers) lives in
``tests.integration.live_stack.spine`` and is shared with the remote spine; this module keeps the
local-libvirt profile factories, the spine body, the RBAC-negative wire tests, and the
local-libvirt owned-infra teardown check.

Acceptance asserted over the wire / against the stack's Postgres + MinIO: protocol (well-formed
envelopes, JWKS-validated tokens), #1 (redacted vmcore in MinIO), #2 (audit per transition +
force_crash, split by attributing principal — driver vs ``system:reconciler``), #3 (redaction does
not leak through the wire), #5 (``torn_down`` + ``Discovery.list_owned()`` empty), the report phase
(``accounting.report_all_projects`` under a ``platform_auditor`` token, windowed to this run —
ADR-0046), and the RBAC negatives (viewer raised-path; operator force_crash ``authorization_denied``
envelope; project-only token denied the all-projects report).

The shared phase-naming contract has its own non-gated unit tests in
``tests/integration/live_stack/test_spine.py``; the spine + RBAC tests here are
``live_stack``-marked and skip without a stack.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.harness import (
    LiveStackClient,
    LiveStackToolError,
    OidcIssuer,
)
from tests.integration.live_stack.spine import (
    SpinePhaseError,
    assert_audit,
    assert_report,
    await_system_state,
    drain_job,
    grant_force_crash_scope,
    mint_role_token,
    ok,
    phase,
    scalar,
    seed_metering,
    system_torn_down,
)

_GUEST_IMAGE_ENV = "KDIVE_GUEST_IMAGE"
_KERNEL_TREE_ENV = "KDIVE_KERNEL_SRC"
_DATABASE_URL_ENV = "KDIVE_DATABASE_URL"
_PROJECT = "spine-proj"
_AGENT_SESSION = "spine-sess"
_ARTIFACT_NAME = "accounting-report.json"


# --- preflight helpers ----------------------------------------------------------------------


def _spine_preflight() -> tuple[OidcIssuer, str, str]:
    """Resolve issuer + stack URL + DB URL, or skip with the exact fix (ADR-0035 §4)."""
    image = os.environ.get(_GUEST_IMAGE_ENV)
    if not image or not Path(image).exists():
        pytest.skip(f"{_GUEST_IMAGE_ENV} unset or missing; run `python -m kdive build-rootfs`")
    tree = os.environ.get(_KERNEL_TREE_ENV)
    if not tree or not Path(tree).exists():
        pytest.skip(
            f"{_KERNEL_TREE_ENV} unset or missing; run the fetch-kernel-tree fixture script"
        )
    db_url = os.environ.get(_DATABASE_URL_ENV)
    if not db_url:
        pytest.skip(f"{_DATABASE_URL_ENV} unset; bring up the stack (see the live-stack runbook)")
    issuer = require_issuer()  # skips if the OIDC issuer is unset/unreachable
    base_url = require_stack()  # skips if KDIVE_STACK_BASE_URL is unset
    return issuer, base_url, db_url


def _wire_preflight() -> tuple[OidcIssuer, str]:
    """Resolve issuer + stack URL for the RBAC-negative wire checks (no VM, no DB).

    These tests exercise a denial that fires in the auth/RBAC layer before any provisioning or DB
    read, so they need only a reachable issuer and a running server — not the guest image / kernel
    tree the booting spine requires. Gating them behind ``_spine_preflight`` would make them skip
    forever on any host without the (currently unbuildable) VM fixtures.
    """
    issuer = require_issuer()  # skips if the OIDC issuer is unset/unreachable
    base_url = require_stack()  # skips if KDIVE_STACK_BASE_URL is unset
    return issuer, base_url


# --- per-role tokens + profiles -------------------------------------------------------------


def _token(issuer: OidcIssuer, *, role: str, platform_roles: list[str] | None = None) -> str:
    return mint_role_token(
        issuer,
        project=_PROJECT,
        agent_session=_AGENT_SESSION,
        role=role,
        platform_roles=platform_roles,
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
                "rootfs": {"kind": "local", "path": os.environ[_GUEST_IMAGE_ENV]},
                "crashkernel": "256M",
                "destructive_ops": ["force_crash"],
            }
        },
    }


def _build_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
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
                    "allocations.request",
                    project=_PROJECT,
                    request={"vcpus": 1, "memory_gb": 1, "resource": {"mode": "kind"}},
                )

    asyncio.run(_run())


@pytest.mark.live_stack
def test_report_all_projects_denied_to_project_token() -> None:
    """A project-only token is denied accounting.report_all_projects over the wire.

    Verified against the tool: the all-projects form catches the raised AuthorizationError and
    *returns* ToolResponse.failure(..., AUTHORIZATION_DENIED) — a well-formed error envelope, not a
    raised tool error. So assert the envelope shape (like crash-rbac-negative), not a raised
    LiveStackToolError (ADR-0046 §3).
    """
    issuer, base_url = _wire_preflight()

    async def _run() -> None:
        project_only = LiveStackClient.over_http(base_url, _token(issuer, role="viewer"))
        async with project_only:
            denied = await scalar(project_only, "accounting.report_all_projects")
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
        from tests.integration.live_stack.spine import db_now  # noqa: PLC0415

        op = LiveStackClient.over_http(base_url, operator_token)
        admin = LiveStackClient.over_http(base_url, admin_token)
        system_id = allocation_id = run_id = ""
        async with op, admin:
            # out-of-band: meter the project (admission is fail-closed, ADR-0046 §0), then capture
            # the report window start from the DB clock (shares ledger.ts's clock).
            await seed_metering(db_url, _PROJECT)
            window_start = await db_now(db_url)
            async with phase("allocate"):
                env = ok(
                    await scalar(
                        op,
                        "allocations.request",
                        project=_PROJECT,
                        request={
                            "vcpus": 2,
                            "memory_gb": 2,
                            "disk_gb": 10,
                            "resource": {"mode": "kind"},
                        },
                    ),
                    "allocate",
                )
                allocation_id = env.object_id
            # out-of-band: grant the destructive capability scope (ADR-0045 §1)
            await grant_force_crash_scope(db_url, allocation_id)
            async with phase("provision"):
                env = ok(
                    await scalar(
                        op,
                        "systems.provision",
                        allocation_id=allocation_id,
                        profile=_provision_profile(),
                    ),
                    "provision",
                )
                system_id = env.data["system_id"]  # in data, NOT object_id (the job id)
                await await_system_state(op, "provision", system_id, "ready")
            async with phase("open-investigation"):
                env = ok(
                    await scalar(op, "investigations.open", project=_PROJECT, title="spine"),
                    "open-investigation",
                )
                investigation_id = env.object_id
            async with phase("create-run"):
                env = ok(
                    await scalar(
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
                    env = ok(await scalar(op, f"runs.{step}", run_id=run_id), step)
                    await drain_job(op, step, env.object_id)
            async with phase("attach"):
                env = ok(
                    await scalar(op, "debug.start_session", run_id=run_id, transport="gdbstub"),
                    "attach",
                )
                session_id = env.object_id
                ok(
                    await scalar(
                        op, "debug.read_registers", session_id=session_id, registers=["rip"]
                    ),
                    "attach",
                )
            async with phase("crash-rbac-negative"):
                denied = await scalar(op, "control.force_crash", system_id=system_id)
                if denied.status != "error" or denied.error_category != "authorization_denied":
                    raise SpinePhaseError("crash-rbac-negative", "operator was not denied")
            async with phase("crash"):
                ok(await scalar(admin, "control.force_crash", system_id=system_id), "crash")
                await await_system_state(admin, "crash", system_id, "crashed")
            async with phase("capture"):
                env = ok(await scalar(op, "vmcore.fetch", system_id=system_id), "capture")
                await drain_job(op, "capture", env.object_id)
                cores = await op.call_tool("vmcore.list", system_id=system_id)
                assert isinstance(cores, list) and cores, "no vmcore artifact listed (#1)"
                refs = [v for c in cores for v in c.refs.values()]
                assert refs, "no vmcore refs (#1)"
                # A raw core is `.../vmcore-{method}` (no `-redacted`); it must never surface.
                assert all(not ("/vmcore-" in r and not r.endswith("-redacted")) for r in refs), (
                    "raw vmcore leaked (#1)"
                )
            async with phase("introspect"):
                env = ok(await scalar(op, "introspect.from_vmcore", run_id=run_id), "introspect")
                report = env.data.get("report", "")
                assert "hunter2" not in report and "password=" not in report, "secret leaked (#3)"
            async with phase("release"):
                ok(
                    await scalar(op, "allocations.release", allocation_id=allocation_id),
                    "release",
                )
            async with phase("teardown"):  # reconciler-driven (≥30s) → torn_down
                await await_system_state(op, "teardown", system_id, "torn_down")
            async with phase("report"):  # all-projects rollup under platform_auditor
                await assert_report(
                    base_url,
                    auditor_token,
                    db_url,
                    window_start,
                    project=_PROJECT,
                    artifact_name=_ARTIFACT_NAME,
                )

        await assert_audit(
            db_url, project=_PROJECT, allocation_id=allocation_id, system_id=system_id
        )
        await _assert_teardown(db_url, system_id)

    asyncio.run(_run())


async def _assert_teardown(db_url: str, system_id: str) -> None:
    """#5: after teardown the System is torn_down and no local-libvirt OwnedInfra remains."""
    assert await system_torn_down(db_url, system_id), "system not torn_down (#5)"
    import libvirt  # noqa: PLC0415 — only importable on a libvirt host (the live_stack path)

    from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery  # noqa: PLC0415

    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: libvirt.open("qemu:///system"),  # ty: ignore[invalid-argument-type]
        concurrent_allocation_cap=2,
    )
    owned_ids = {o["system_id"] for o in disc.list_owned()}
    assert system_id not in owned_ids, "released system still owned (#5)"
