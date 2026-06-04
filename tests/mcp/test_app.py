"""app.py: tool registration via the seam, with an injected verifier."""

from __future__ import annotations

import asyncio

from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.mcp.app import build_app, build_handler_registry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


def _verifier() -> JWTVerifier:
    kp = make_keypair()
    return JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)


def test_build_app_registers_jobs_tools() -> None:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier())

    async def _run() -> None:
        # Verified against fastmcp 3.4.0: FastMCP.list_tools() is async and returns
        # list[Tool], each with a .name (there is no get_tools()).
        tools = await app.list_tools()
        names = {t.name for t in tools}
        assert {"jobs.get", "jobs.wait", "jobs.cancel", "jobs.list"} <= names
        assert {"systems.provision", "systems.get", "systems.teardown"} <= names
        assert {
            "investigations.open",
            "investigations.get",
            "investigations.close",
            "investigations.link",
            "investigations.unlink",
        } <= names
        assert {"runs.create", "runs.get", "runs.build", "runs.install", "runs.boot"} <= names
        assert {"control.power", "control.force_crash"} <= names
        assert {
            "vmcore.fetch",
            "vmcore.list",
            "artifacts.list",
            "artifacts.get",
            "postmortem.crash",
            "postmortem.triage",
        } <= names
        assert {"introspect.from_vmcore"} <= names

    asyncio.run(_run())


def test_build_app_produces_a_streamable_http_asgi_app() -> None:
    # The server entrypoint serves build_app(...).http_app() over streamable HTTP;
    # assert the ASGI app assembles (no DB/network needed) so the run path is covered
    # beyond tool registration.
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier())
    asgi = app.http_app()
    assert callable(asgi)


def test_build_handler_registry_binds_provisioning_and_build_handlers() -> None:
    # The provisioning plane (#16) registers provision/teardown, the build plane (#18)
    # registers build, the install + boot plane (#19) registers install/boot, and the
    # retrieve plane (#24) registers capture_vmcore — each building its provider/builder
    # lazily from env (no libvirt/S3/toolchain connection at registration).
    registry = build_handler_registry()
    assert isinstance(registry, HandlerRegistry)
    assert registry.get(JobKind.PROVISION) is not None
    assert registry.get(JobKind.TEARDOWN) is not None
    assert registry.get(JobKind.BUILD) is not None
    assert registry.get(JobKind.INSTALL) is not None
    assert registry.get(JobKind.BOOT) is not None
    assert registry.get(JobKind.CAPTURE_VMCORE) is not None
