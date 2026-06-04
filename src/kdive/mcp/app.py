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
from kdive.mcp.tools import allocations, investigations, jobs, resources, runs, systems

# Tool seam: each plane exposes register(app, pool); build_app calls them all.
_PLANE_REGISTRARS: tuple[Callable[[FastMCP, AsyncConnectionPool], None], ...] = (
    jobs.register,
    resources.register,
    allocations.register,
    systems.register,
    investigations.register,
    runs.register,
)

# Handler seam: each plane exposes register_handlers(registry); the worker calls them all.
# jobs.* register no JobHandler; the provisioning plane (#16) registers the provision/teardown
# handlers and the build plane (#18) registers the build handler (each builds its provider/
# builder lazily from env — no libvirt/toolchain connection at registration).
_HANDLER_REGISTRARS: tuple[Callable[[HandlerRegistry], None], ...] = (
    systems.register_handlers,
    runs.register_handlers,
)


def build_app(pool: AsyncConnectionPool, *, verifier: JWTVerifier | None = None) -> FastMCP:
    """Construct the FastMCP app and register every plane's tools.

    Args:
        pool: The shared async connection pool tools read through.
        verifier: An injected verifier (tests pass a local-keypair one); when
            ``None``, built from the OIDC env vars via :func:`build_verifier`.
    """
    app: FastMCP = FastMCP(name="kdive", auth=verifier or build_verifier())
    for register in _PLANE_REGISTRARS:
        register(app, pool)
    return app


def build_handler_registry() -> HandlerRegistry:
    """Build the worker's `HandlerRegistry` from the handler seam (empty in M0)."""
    registry = HandlerRegistry()
    for register in _HANDLER_REGISTRARS:
        register(registry)
    return registry
