"""FastMCP application assembly and the two plane registrar seams (issue #10).

A plane issue (#11+) ships a tool surface *and* a job handler. The skeleton exposes
two symmetric seams so a plane is added by appending to a tuple here and never edits
the entrypoint: `_PLANE_REGISTRARS` (tools, called by :func:`build_app`) and
`_HANDLER_REGISTRARS` (worker job handlers, called by :func:`build_handler_registry`).
Both are empty of non-jobs planes in M0; `jobs.*` register tools but no job handler
(they are read/cancel tools, not job kinds).
"""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.jobs.models import HandlerRegistry
from kdive.mcp.auth import build_verifier
from kdive.mcp.tools import (
    accounting,
    allocations,
    artifacts,
    control,
    debug,
    introspect,
    investigations,
    jobs,
    resources,
    runs,
    systems,
    vmcore,
)
from kdive.providers.composition import ProviderRuntime, build_default_provider_runtime

type PlaneRegistrar = Callable[[FastMCP, AsyncConnectionPool, ProviderRuntime], None]
type HandlerRegistrar = Callable[[HandlerRegistry, ProviderRuntime], None]


def _plain(register: Callable[[FastMCP, AsyncConnectionPool], None]) -> PlaneRegistrar:
    def _register(app: FastMCP, pool: AsyncConnectionPool, _: ProviderRuntime) -> None:
        register(app, pool)

    return _register


# Tool seam: each plane exposes register(app, pool); provider-aware planes receive the runtime.
_PLANE_REGISTRARS: tuple[PlaneRegistrar, ...] = (
    _plain(jobs.register),
    _plain(resources.register),
    _plain(accounting.register),
    _plain(allocations.register),
    _plain(systems.register),
    _plain(investigations.register),
    _plain(runs.register),
    _plain(control.register),
    _plain(artifacts.register),
    lambda app, pool, runtime: vmcore.register(app, pool, provider_runtime=runtime),
    lambda app, pool, runtime: debug.register(app, pool, provider_runtime=runtime),
    lambda app, pool, runtime: introspect.register(app, pool, provider_runtime=runtime),
)

# Handler seam: each plane exposes register_handlers(registry); the worker calls them all.
# jobs.* register no JobHandler; the provisioning plane (#16) registers the provision/teardown
# handlers, the build plane (#18) registers the build handler, the control plane (#23)
# registers the power/force_crash handlers, and the retrieve plane (#24) registers the
# capture_vmcore handler (each builds its provider/builder lazily from env — no libvirt/
# toolchain connection at registration). The Connect plane (#20) registers tools only — its
# debug.start_session/end_session are synchronous, so they have no JobKind and no handler.
_HANDLER_REGISTRARS: tuple[HandlerRegistrar, ...] = (
    lambda registry, runtime: systems.register_handlers(registry, provider_runtime=runtime),
    lambda registry, runtime: runs.register_handlers(registry, provider_runtime=runtime),
    lambda registry, runtime: control.register_handlers(registry, provider_runtime=runtime),
    lambda registry, runtime: vmcore.register_handlers(registry, provider_runtime=runtime),
)


def build_app(
    pool: AsyncConnectionPool,
    *,
    verifier: JWTVerifier | None = None,
    provider_runtime: ProviderRuntime | None = None,
) -> FastMCP:
    """Construct the FastMCP app and register every plane's tools.

    Args:
        pool: The shared async connection pool tools read through.
        verifier: An injected verifier (tests pass a local-keypair one); when
            ``None``, built from the OIDC env vars via :func:`build_verifier`.
    """
    app: FastMCP = FastMCP(name="kdive", auth=verifier or build_verifier())
    runtime = provider_runtime or build_default_provider_runtime()
    for register in _PLANE_REGISTRARS:
        register(app, pool, runtime)
    return app


def build_handler_registry(*, provider_runtime: ProviderRuntime | None = None) -> HandlerRegistry:
    """Build the worker's `HandlerRegistry` from the handler seam (empty in M0)."""
    registry = HandlerRegistry()
    runtime = provider_runtime or build_default_provider_runtime()
    for register in _HANDLER_REGISTRARS:
        register(registry, runtime)
    return registry
