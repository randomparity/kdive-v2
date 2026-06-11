"""Operator-run remote-libvirt spine e2e (#207, M2 issue 8; mirrors ADR-0042's local spine).

Drives allocate(remote-libvirt) → provision(disk-image) → build → install → boot →
attach(gdb-MI direct TCP) → force-crash → two-phase KDUMP vmcore capture →
introspect(from_vmcore) → release → (reconciler) teardown → accounting report, over the live MCP
HTTP transport under per-project role tokens, against a genuinely remote ``qemu+tls://`` host the
server/worker tier does not share a filesystem with. It is ``live_stack``-marked and preflights to
a clean skip unless the remote provider config + a reachable stack + issuer + DB are all present
(CI deselects ``live_stack``). The shared spine scaffolding lives in
``tests.integration.live_stack.spine``; this module adds the remote preflight, the disk-image
profile factory, and the remote spine body.

Two non-gated unit tests pin the CI-runnable surface: the remote profile factory parses through
the real validator (the disk-image↔remote-section pairing rule), and the preflight skips with an
actionable reason when the provider config is absent.

Out of scope (deferred ADR-0083 follow-up #215): in-guest drgn-*live* MCP routing. The introspect
phase here is the worker-side vmcore postmortem (``introspect.from_vmcore``), which resolves the
per-run runtime via ``with_runtime_for_run`` and so routes to the remote runtime's postmortem.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from kdive.images.planes.remote_libvirt import REMOTE_BASE_IMAGE_NAME
from kdive.profiles.provisioning import ProvisioningProfile
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.harness import LiveStackClient, OidcIssuer
from tests.integration.live_stack.spine import (
    SpinePhaseError,
    assert_report,
    await_system_state,
    db_now,
    drain_job,
    grant_force_crash_scope,
    mint_role_token,
    ok,
    phase,
    scalar,
    seed_metering,
)

_REMOTE_URI_ENV = "KDIVE_REMOTE_LIBVIRT_URI"
# Test/runbook input feeding the provision profile's base_image_volume — NOT provider config.
_BASE_IMAGE_ENV = "KDIVE_REMOTE_BASE_IMAGE_VOLUME"
_KERNEL_TREE_ENV = "KDIVE_KERNEL_SRC"
_DATABASE_URL_ENV = "KDIVE_DATABASE_URL"
_PROJECT = "remote-spine-proj"
_AGENT_SESSION = "remote-spine-sess"
_ARTIFACT_NAME = "remote-accounting-report.json"
_DEFAULT_KERNEL_REF = "git+https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git#v6.9"

# The remote capture job waits out a ~300s server-side readiness window (retrieve.py) while the
# guest reboots out of the kdump capture kernel, then uploads via the presigned PUT; budget the
# drain above that plus the reboot.
_CAPTURE_DEADLINE_S = 900.0

# gdb_addr has no default and remote provisioning fails closed without it
# (providers/remote_libvirt/provisioning.py), so a host missing it must skip — not fail at
# provision. Require these alongside the URI for the clean-skip contract.
_REQUIRED_REFS = (
    "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF",
    "KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF",  # noqa: S105 — env-var name, not a secret
    "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF",
    "KDIVE_REMOTE_LIBVIRT_GDB_ADDR",
)


def _remote_provision_profile() -> dict[str, object]:
    """The disk-image remote profile (ADR-0080); force_crash opted in (the gate's profile factor).

    Returns the profile as a dict for the wire; the unit test parses it through the validator.
    """
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": 20,
        "boot_method": "disk-image",
        "kernel_source_ref": os.environ.get(_KERNEL_TREE_ENV, _DEFAULT_KERNEL_REF),
        "provider": {
            "remote-libvirt": {
                "base_image_volume": os.environ.get(
                    _BASE_IMAGE_ENV, f"{REMOTE_BASE_IMAGE_NAME}.qcow2"
                ),
                "crashkernel": "256M",
                "destructive_ops": ["force_crash"],
            }
        },
    }


def _build_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kernel_source_ref": os.environ.get(_KERNEL_TREE_ENV, _DEFAULT_KERNEL_REF),
        "config": {"kind": "local", "path": "/configs/kdump.config"},
    }


def _remote_spine_preflight() -> tuple[OidcIssuer, str, str]:
    """Resolve issuer + stack URL + DB URL for the remote spine, or skip with the exact fix."""
    if not os.environ.get(_REMOTE_URI_ENV):
        pytest.skip(
            f"{_REMOTE_URI_ENV} unset; configure the remote-libvirt host "
            "(see docs/runbooks/remote-live-stack.md)"
        )
    for ref_env in _REQUIRED_REFS:
        if not os.environ.get(ref_env):
            pytest.skip(
                f"{ref_env} unset; stage the TLS cert refs + gdbstub ACL address "
                "(remote-live-stack runbook)"
            )
    if not os.environ.get(_BASE_IMAGE_ENV):
        pytest.skip(
            f"{_BASE_IMAGE_ENV} unset; stage the base-OS volume (remote-live-stack runbook)"
        )
    db_url = os.environ.get(_DATABASE_URL_ENV)
    if not db_url:
        pytest.skip(f"{_DATABASE_URL_ENV} unset; bring up the stack (remote-live-stack runbook)")
    issuer = require_issuer()  # skips if the OIDC issuer is unset/unreachable
    base_url = require_stack()  # skips if KDIVE_STACK_BASE_URL is unset
    return issuer, base_url, db_url


def _token(issuer: OidcIssuer, *, role: str, platform_roles: list[str] | None = None) -> str:
    return mint_role_token(
        issuer,
        project=_PROJECT,
        agent_session=_AGENT_SESSION,
        role=role,
        platform_roles=platform_roles,
    )


# --- non-gated unit tests (CI-runnable; pin the preflight + profile shape) -------------------


def test_remote_provision_profile_validates() -> None:
    """The remote profile factory parses through the real validator (disk-image↔remote pairing)."""
    profile = ProvisioningProfile.parse(_remote_provision_profile())
    assert profile.boot_method.value == "disk-image"
    assert profile.provider.remote_libvirt.base_image_volume


def test_remote_provision_default_references_the_built_image_not_a_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provision default names the kdive-published base image, not the placeholder literal.

    The ADR-0080 remote plane shipped a placeholder base-image volume (``kdive-base.qcow2``).
    M2.4/3 produces a real built image; the provision default now derives from the plane's
    published base-image name (its identity is the qcow2 content digest, ADR-0092).
    """
    monkeypatch.delenv(_BASE_IMAGE_ENV, raising=False)
    profile = ProvisioningProfile.parse(_remote_provision_profile())
    default_volume = profile.provider.remote_libvirt.base_image_volume
    assert default_volume != "kdive-base.qcow2", "the placeholder volume literal is removed"
    assert REMOTE_BASE_IMAGE_NAME in default_volume


def test_remote_preflight_skips_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the remote provider URI unset, the preflight skips with the actionable reason."""
    monkeypatch.delenv(_REMOTE_URI_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        _remote_spine_preflight()
    assert _REMOTE_URI_ENV in str(excinfo.value)


# --- the full remote spine ------------------------------------------------------------------


@pytest.mark.live_stack
def test_remote_spine_over_the_wire() -> None:
    """Drive allocate(remote) → … → report over HTTP; assert capture/introspect; name the phase."""
    issuer, base_url, db_url = _remote_spine_preflight()
    operator_token = _token(issuer, role="operator")
    admin_token = _token(issuer, role="admin")
    auditor_token = _token(issuer, role="viewer", platform_roles=["platform_auditor"])

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, operator_token)
        admin = LiveStackClient.over_http(base_url, admin_token)
        system_id = allocation_id = run_id = ""
        async with op, admin:
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
                            "resource": {"mode": "kind", "kind": "remote-libvirt"},
                        },
                    ),
                    "allocate",
                )
                allocation_id = env.object_id
            await grant_force_crash_scope(db_url, allocation_id)
            async with phase("provision"):
                env = ok(
                    await scalar(
                        op,
                        "systems.provision",
                        allocation_id=allocation_id,
                        profile=_remote_provision_profile(),
                    ),
                    "provision",
                )
                system_id = env.data["system_id"]  # in data, NOT object_id (the job id)
                await await_system_state(op, "provision", system_id, "ready")
            async with phase("open-investigation"):
                env = ok(
                    await scalar(op, "investigations.open", project=_PROJECT, title="remote-spine"),
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
                # Remote is KDUMP-only (ADR-0084); pin the method (fetch defaults to host_dump).
                env = ok(
                    await scalar(op, "vmcore.fetch", system_id=system_id, method="kdump"),
                    "capture",
                )
                await drain_job(op, "capture", env.object_id, deadline_s=_CAPTURE_DEADLINE_S)
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
                assert report, "empty postmortem report (introspect did not route to remote run)"
                assert "hunter2" not in report and "password=" not in report, "secret leaked (#3)"
            async with phase("release"):
                ok(
                    await scalar(op, "allocations.release", allocation_id=allocation_id),
                    "release",
                )
            async with phase("teardown"):  # reconciler-driven (≥30s) → torn_down
                await await_system_state(op, "teardown", system_id, "torn_down")
            async with phase("report"):  # all-projects rollup; writes the evidence artifact
                await assert_report(
                    base_url,
                    auditor_token,
                    db_url,
                    window_start,
                    project=_PROJECT,
                    artifact_name=_ARTIFACT_NAME,
                )

    asyncio.run(_run())
