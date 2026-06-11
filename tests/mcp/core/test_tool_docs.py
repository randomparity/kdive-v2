"""The ADR-0047 documentation guard, over the live FastMCP registry.

Builds the app with a null pool + a local-keypair verifier (the service-test
path; needs no DB and no OIDC env), then asserts every tool is fully
documented, the destructive hint matches the reviewed set, and every
`implemented` or `partial` tool is assigned to a non-live behavior test module.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast, get_type_hints

from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools.function_tool import FunctionTool
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.app import build_app
from kdive.mcp.tools import _docmeta
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair

_HERE = Path(__file__).resolve()
_REPO_ROOT = next(parent for parent in _HERE.parents if (parent / "pyproject.toml").is_file())
_NON_LIVE_MARKERS = ("pytest.mark.live_vm", "pytest.mark.live_stack")
_BEHAVIOR_TESTS_BY_TOOL = {
    "accounting.estimate": ("tests/mcp/accounting/test_accounting_tools.py",),
    "accounting.report_all_projects": ("tests/mcp/accounting/test_accounting_report.py",),
    "accounting.report_granted_set": ("tests/mcp/accounting/test_accounting_report.py",),
    "accounting.set_budget": ("tests/mcp/accounting/test_accounting_admin_tools.py",),
    "accounting.set_quota": ("tests/mcp/accounting/test_accounting_admin_tools.py",),
    "accounting.usage_investigation": ("tests/mcp/accounting/test_accounting_usage.py",),
    "accounting.usage_project": ("tests/mcp/accounting/test_accounting_usage.py",),
    "allocations.get": ("tests/mcp/lifecycle/test_allocations_tools.py",),
    "allocations.list": ("tests/mcp/lifecycle/test_allocations_tools.py",),
    "allocations.release": ("tests/mcp/lifecycle/test_allocations_reconcile.py",),
    "allocations.renew": ("tests/mcp/lifecycle/test_allocations_renew.py",),
    "allocations.request": ("tests/mcp/lifecycle/test_allocations_tools.py",),
    "artifacts.create_run_upload": ("tests/mcp/lifecycle/test_create_upload_tool.py",),
    "artifacts.create_system_upload": ("tests/mcp/lifecycle/test_create_upload_tool.py",),
    "artifacts.get": ("tests/mcp/catalog/test_artifacts_tools.py",),
    "artifacts.list": ("tests/mcp/catalog/test_artifacts_tools.py",),
    "artifacts.search_text": ("tests/mcp/catalog/test_artifacts_tools.py",),
    "audit.query": ("tests/mcp/ops/test_audit_query.py",),
    "control.force_crash": ("tests/mcp/lifecycle/test_control_tools.py",),
    "control.power": ("tests/mcp/lifecycle/test_control_tools.py",),
    "debug.clear_breakpoint": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.continue": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.end_session": (
        "tests/mcp/debug/test_debug_tools.py",
        "tests/mcp/debug/test_debug_ops.py",
    ),
    "debug.interrupt": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.list_breakpoints": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.read_memory": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.read_registers": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.set_breakpoint": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.start_session": ("tests/mcp/debug/test_debug_tools.py",),
    "fixtures.list": ("tests/mcp/catalog/test_fixtures_list.py",),
    "inventory.list": ("tests/mcp/ops/test_inventory_list.py",),
    "investigations.close": ("tests/mcp/catalog/test_investigations_tools.py",),
    "investigations.get": ("tests/mcp/catalog/test_investigations_tools.py",),
    "investigations.link": ("tests/mcp/catalog/test_investigations_tools.py",),
    "investigations.open": ("tests/mcp/catalog/test_investigations_tools.py",),
    "investigations.unlink": ("tests/mcp/catalog/test_investigations_tools.py",),
    "jobs.cancel": ("tests/mcp/catalog/test_jobs_tools.py",),
    "jobs.get": ("tests/mcp/catalog/test_jobs_tools.py",),
    "jobs.list": ("tests/mcp/catalog/test_jobs_tools.py",),
    "jobs.wait": ("tests/mcp/catalog/test_jobs_tools.py",),
    "introspect.from_vmcore": ("tests/mcp/debug/test_introspect_tools.py",),
    "introspect.run": ("tests/mcp/debug/test_introspect_tools.py",),
    "ops.diagnostics": ("tests/mcp/ops/test_diagnostics.py",),
    "ops.force_release": ("tests/mcp/ops/test_breakglass.py",),
    "ops.force_teardown": ("tests/mcp/ops/test_breakglass.py",),
    "ops.jobs_list": ("tests/mcp/ops/test_queue_tools.py",),
    "ops.queue_pause": ("tests/mcp/ops/test_queue_tools.py",),
    "ops.queue_resume": ("tests/mcp/ops/test_queue_tools.py",),
    "ops.reconcile_now": ("tests/mcp/ops/test_reconcile_now.py",),
    "ops.set_cost_class_coeff": ("tests/mcp/ops/test_ops_tuning.py",),
    "ops.set_host_capacity": ("tests/mcp/ops/test_ops_tuning.py",),
    "resources.availability": ("tests/mcp/catalog/test_availability_tools.py",),
    "resources.cordon": ("tests/mcp/catalog/test_resources_tools.py",),
    "resources.describe": ("tests/mcp/catalog/test_resources_tools.py",),
    "resources.drain": ("tests/mcp/catalog/test_resources_tools.py",),
    "resources.list": ("tests/mcp/catalog/test_resources_tools.py",),
    "resources.set_status": ("tests/mcp/catalog/test_resources_tools.py",),
    "resources.uncordon": ("tests/mcp/catalog/test_resources_tools.py",),
    "postmortem.crash": ("tests/mcp/lifecycle/test_vmcore_tools.py",),
    "postmortem.triage": ("tests/mcp/lifecycle/test_vmcore_tools.py",),
    "runs.boot": ("tests/mcp/lifecycle/test_runs_tools.py",),
    "runs.build": ("tests/mcp/lifecycle/test_runs_tools.py",),
    "runs.complete_build": ("tests/mcp/lifecycle/test_complete_build_tool.py",),
    "runs.create": ("tests/mcp/lifecycle/test_runs_tools.py",),
    "runs.get": ("tests/mcp/lifecycle/test_runs_tools.py",),
    "runs.install": ("tests/mcp/lifecycle/test_runs_tools.py",),
    "secrets.list": ("tests/mcp/ops/test_secrets_list.py",),
    "shapes.delete": ("tests/mcp/catalog/test_shapes_tools.py",),
    "shapes.list": ("tests/mcp/catalog/test_shapes_tools.py",),
    "shapes.set": ("tests/mcp/catalog/test_shapes_tools.py",),
    "systems.define": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "systems.get": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "systems.list": ("tests/mcp/lifecycle/test_systems_list.py",),
    "systems.provision": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "systems.provision_defined": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "systems.reprovision": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "systems.teardown": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "vmcore.fetch": ("tests/mcp/lifecycle/test_vmcore_tools.py",),
    "vmcore.list": ("tests/mcp/lifecycle/test_vmcore_tools.py",),
}


def _build_tools() -> list[FunctionTool]:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    # list_tools() is typed as Sequence[mcp.types.Tool] but the fastmcp runtime
    # returns list[FunctionTool] — cast to the concrete type so the rest of the
    # module can access .fn / .meta / .annotations without type errors.
    return cast(list[FunctionTool], asyncio.run(app.list_tools()))


def _reaches_symbol(fn: Callable[..., Any], target: str) -> bool:
    """Whether ``fn`` calls ``target`` directly or via a delegate it transitively calls.

    The `@app.tool` wrappers are 1:1 delegators: the security-relevant call
    (``assert_destructive_allowed``) lives one frame deeper, in the module-level handler the
    wrapper invokes (`force_crash_system`, `reprovision_system`), never in the wrapper body.
    Parsing only ``fn`` would miss it — so follow each called ``Name`` that resolves to a
    function in ``fn``'s own module globals (a nested closure still carries its module's
    globals). Termination is the ``seen`` set over the finite function graph; there is no
    depth cap, because a numeric horizon would silently fail open (report "no gate reached")
    for a call buried below it — the very vacuity this backstop exists to prevent.
    """
    seen: set[int] = set()

    def _method_from_factory_return(
        factory: Callable[..., Any],
        attr: str,
        nonlocals: Mapping[str, Any],
    ) -> Callable[..., Any] | None:
        if inspect.ismethod(factory):
            factory = factory.__func__
        if not inspect.isfunction(factory):
            return None
        try:
            hints = get_type_hints(factory, globalns=factory.__globals__, localns=nonlocals)
        except (NameError, TypeError):
            return None
        owner_type = hints.get("return")
        delegate = getattr(owner_type, attr, None)
        return delegate if callable(delegate) else None

    def _walk(f: Callable[..., Any]) -> bool:
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(f)))
        except (OSError, TypeError):
            return False
        glb = getattr(f, "__globals__", {})
        try:
            nonlocals = inspect.getclosurevars(f).nonlocals
        except TypeError:
            nonlocals = {}
        local_calls: set[str] = set()
        attribute_calls: list[Callable[..., Any]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                callee = node.func
                if isinstance(callee, ast.Name):
                    if callee.id == target:
                        return True
                    local_calls.add(callee.id)
                elif isinstance(callee, ast.Attribute) and callee.attr == target:
                    return True
                elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
                    owner = nonlocals.get(callee.value.id, glb.get(callee.value.id))
                    delegate = getattr(owner, callee.attr, None)
                    if callable(delegate):
                        attribute_calls.append(delegate)
                elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Call):
                    factory_call = callee.value.func
                    if isinstance(factory_call, ast.Name):
                        factory = nonlocals.get(factory_call.id, glb.get(factory_call.id))
                        if callable(factory):
                            delegate = _method_from_factory_return(factory, callee.attr, nonlocals)
                            if delegate is not None:
                                attribute_calls.append(delegate)
                    elif isinstance(factory_call, ast.Attribute) and isinstance(
                        factory_call.value, ast.Name
                    ):
                        owner = nonlocals.get(factory_call.value.id, glb.get(factory_call.value.id))
                        factory = getattr(owner, factory_call.attr, None)
                        if callable(factory):
                            delegate = _method_from_factory_return(factory, callee.attr, nonlocals)
                            if delegate is not None:
                                attribute_calls.append(delegate)
        for name in local_calls:
            delegate = glb.get(name)
            if inspect.isfunction(delegate) and id(delegate) not in seen:
                seen.add(id(delegate))
                if _walk(delegate):
                    return True
        for delegate in attribute_calls:
            if id(delegate) not in seen:
                seen.add(id(delegate))
                if _walk(delegate):
                    return True
        return False

    return _walk(fn)


TOOLS = _build_tools()


def test_every_tool_has_a_description() -> None:
    missing = [t.name for t in TOOLS if not (t.description or "").strip()]
    assert not missing, f"tools missing a description: {missing}"


def test_every_parameter_has_a_description() -> None:
    offenders: list[str] = []
    for t in TOOLS:
        props = (t.parameters or {}).get("properties", {})
        for param, schema in props.items():
            if not (schema.get("description") or "").strip():
                offenders.append(f"{t.name}:{param}")
    assert not offenders, f"parameters missing a description: {offenders}"


def test_run_cmdline_docs_describe_debug_args_only() -> None:
    """The agent-provided cmdline must not document platform-owned boot args."""
    tools = {t.name: t for t in TOOLS}
    for tool_name in ("runs.build", "runs.complete_build"):
        schema = tools[tool_name].parameters["properties"]["cmdline"]
        description = schema["description"]
        assert "dhash_entries=1" in description
        assert "console=ttyS0" not in description
        assert "root=/dev/vda" not in description


def test_every_tool_has_a_valid_maturity() -> None:
    valid = {"implemented", "partial", "planned"}
    offenders = [t.name for t in TOOLS if (t.meta or {}).get("maturity") not in valid]
    assert not offenders, f"tools with missing/invalid maturity: {offenders}"


def test_destructive_hint_matches_reviewed_set() -> None:
    hinted = {t.name for t in TOOLS if (t.annotations and t.annotations.destructiveHint)}
    assert hinted == _docmeta.DESTRUCTIVE_TOOLS, (
        f"destructiveHint set {sorted(hinted)} != reviewed set {sorted(_docmeta.DESTRUCTIVE_TOOLS)}"
    )


def _gate_reachers() -> set[str]:
    """Tools whose wrapper reaches ``assert_destructive_allowed`` (through its delegate)."""
    return {t.name for t in TOOLS if _reaches_symbol(t.fn, "assert_destructive_allowed")}


def test_gate_callers_are_in_the_destructive_set() -> None:
    # Backstop: any tool that reaches assert_destructive_allowed must be in the reviewed
    # set (the converse — admin-gated ops — is not asserted). The reach is transitive: the
    # gate lives in the module-level handler the wrapper delegates to, not in the wrapper.
    gate_reachers = _gate_reachers()
    assert gate_reachers <= _docmeta.DESTRUCTIVE_TOOLS, (
        f"gate-calling tools not in the destructive set: "
        f"{sorted(gate_reachers - _docmeta.DESTRUCTIVE_TOOLS)}"
    )


def test_backstop_actually_detects_the_known_gate_callers() -> None:
    # Canary against a vacuous backstop: the gate-reacher set must be EXACTLY the tools
    # that call assert_destructive_allowed today. Equality (not subset) catches both a broken
    # mechanism — the reach analysis stopping at the wrapper body would empty this set — and
    # an unexpected new reacher, which then must be reviewed into DESTRUCTIVE_TOOLS and pinned
    # here, mirroring test_destructive_tools_set_is_exactly_the_four.
    assert _gate_reachers() == {
        "control.force_crash",
        "control.power",
        "systems.teardown",
        "systems.reprovision",
    }


def test_active_tools_have_a_covering_test() -> None:
    covered_maturities = {"implemented", "partial"}
    active = {t.name for t in TOOLS if (t.meta or {}).get("maturity") in covered_maturities}
    mapped = set(_BEHAVIOR_TESTS_BY_TOOL)
    assert active == mapped, (
        "active tool behavior-test map is out of date: "
        f"missing {sorted(active - mapped)}, stale {sorted(mapped - active)}"
    )

    missing_files: list[str] = []
    live_only_files: list[str] = []
    for tool, rel_paths in _BEHAVIOR_TESTS_BY_TOOL.items():
        for rel_path in rel_paths:
            path = _REPO_ROOT / rel_path
            if not path.is_file():
                missing_files.append(f"{tool}: {rel_path}")
                continue
            text = path.read_text(encoding="utf-8")
            if any(marker in text for marker in _NON_LIVE_MARKERS):
                live_only_files.append(f"{tool}: {rel_path}")
    assert not missing_files, f"mapped behavior test files do not exist: {missing_files}"
    assert not live_only_files, f"mapped behavior tests must be non-live: {live_only_files}"
