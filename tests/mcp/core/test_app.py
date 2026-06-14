"""app.py: tool registration via the seam, with an injected verifier."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

import kdive.mcp.app as app_module
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.mcp.app import build_app, build_handler_registry
from kdive.providers import composition
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


def _verifier() -> JWTVerifier:
    kp = make_keypair()
    return JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)


def test_build_app_registers_jobs_tools() -> None:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())

    async def _run() -> None:
        # Verified against fastmcp 3.4.0: FastMCP.list_tools() is async and returns
        # list[Tool], each with a .name (there is no get_tools()).
        tools = await app.list_tools()
        names = {t.name for t in tools}
        assert {"jobs.get", "jobs.wait", "jobs.cancel", "jobs.list"} <= names
        assert {
            "systems.provision",
            "systems.provision_defined",
            "systems.get",
            "systems.teardown",
            "systems.reprovision",
        } <= names
        assert {
            "investigations.open",
            "investigations.get",
            "investigations.close",
            "investigations.link",
            "investigations.unlink",
        } <= names
        assert {
            "runs.create",
            "runs.get",
            "runs.build",
            "runs.complete_build",
            "runs.install",
            "runs.boot",
        } <= names
        assert {"control.power", "control.force_crash"} <= names
        assert {
            "vmcore.fetch",
            "vmcore.list",
            "artifacts.list",
            "artifacts.get",
            "postmortem.crash",
            "postmortem.triage",
        } <= names
        assert {"debug.start_session", "debug.end_session"} <= names
        assert {
            "debug.set_breakpoint",
            "debug.clear_breakpoint",
            "debug.list_breakpoints",
            "debug.read_memory",
            "debug.read_registers",
            "debug.continue",
            "debug.interrupt",
        } <= names
        assert {"introspect.from_vmcore", "introspect.run"} <= names
        assert {
            "accounting.estimate",
            "accounting.usage_project",
            "accounting.usage_investigation",
            "accounting.report_granted_set",
            "accounting.report_all_projects",
        } <= names
        assert {
            "allocations.request",
            "allocations.get",
            "allocations.release",
            "allocations.renew",
            "allocations.list",
        } <= names

    asyncio.run(_run())


def test_resource_host_and_mutation_tools_have_separate_plane_registrars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _register_host(_app: FastMCP, _pool: AsyncConnectionPool) -> None:
        calls.append("host")

    def _register_mutation(_app: FastMCP, _pool: AsyncConnectionPool) -> None:
        calls.append("mutation")

    monkeypatch.setattr(app_module.ops_resource_host_tools, "register", _register_host)
    monkeypatch.setattr(
        app_module.ops_resource_mutation_tools, "register_mutation_tools", _register_mutation
    )
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    assembly = cast(app_module.AppAssembly, object())

    assert app_module._register_ops_resource_host_tools in app_module._PLANE_REGISTRARS
    assert app_module._register_ops_resource_mutation_tools in app_module._PLANE_REGISTRARS
    app_module._register_ops_resource_host_tools(FastMCP(name="host"), pool, assembly)
    app_module._register_ops_resource_mutation_tools(FastMCP(name="mutation"), pool, assembly)

    assert calls == ["host", "mutation"]


def test_build_app_produces_a_streamable_http_asgi_app() -> None:
    # The server entrypoint serves build_app(...).http_app() over streamable HTTP;
    # assert the ASGI app assembles (no DB/network needed) so the run path is covered
    # beyond tool registration.
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
    asgi = app.http_app()
    assert callable(asgi)


def test_build_app_uses_injected_composition_secret_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[app_module.AppAssembly] = []

    def _capture_assembly(
        app: FastMCP,
        _pool: AsyncConnectionPool,
        assembly: app_module.AppAssembly,
    ) -> None:
        captured.append(assembly)

        # Register one tool so build_app produces a non-empty surface — a real registrar always
        # registers tools, and build_app's flat-schema sweep raises on a zero-tool count (ADR-0113).
        @app.tool(name="_probe")
        def _probe() -> str:
            return "ok"

    monkeypatch.setattr(app_module, "_PLANE_REGISTRARS", (_capture_assembly,))
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    composition_registry = SecretRegistry()
    caller_registry = SecretRegistry()
    provider_composition = composition.ProviderComposition(secret_registry=composition_registry)

    build_app(
        pool,
        verifier=_verifier(),
        provider_composition=provider_composition,
        secret_registry=caller_registry,
    )

    assert captured[0].secret_registry is composition_registry


def test_build_handler_registry_binds_provisioning_and_build_handlers() -> None:
    # The provisioning plane (#16) registers provision/teardown, the build plane (#18)
    # registers build, the install + boot plane (#19) registers install/boot, and the
    # retrieve plane (#24) registers capture_vmcore — each building its provider/builder
    # lazily from env (no libvirt/S3/toolchain connection at registration).
    registry = build_handler_registry(secret_registry=SecretRegistry())
    assert isinstance(registry, HandlerRegistry)
    assert registry.get(JobKind.PROVISION) is not None
    assert registry.get(JobKind.TEARDOWN) is not None
    assert registry.get(JobKind.BUILD) is not None
    assert registry.get(JobKind.INSTALL) is not None
    assert registry.get(JobKind.BOOT) is not None
    assert registry.get(JobKind.CAPTURE_VMCORE) is not None


def test_image_build_handler_preserves_store_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = HandlerRegistry()
    error = CategorizedError(
        "missing image store",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"setting": "KDIVE_S3_ENDPOINT"},
    )

    def _raise_store() -> object:
        raise error

    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", _raise_store)
    app_module._register_image_build_handler(
        registry, cast(Any, None), SecretRegistry(), cast(Any, None)
    )
    handler = registry.get(JobKind.IMAGE_BUILD)
    assert handler is not None

    async def _run() -> None:
        with pytest.raises(CategorizedError) as caught:
            await handler(cast(Any, None), cast(Any, None))
        assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert caught.value.details == {"setting": "KDIVE_S3_ENDPOINT"}

    asyncio.run(_run())
