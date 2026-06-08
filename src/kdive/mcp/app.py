"""FastMCP application assembly and the two plane registrar seams (issue #10).

A plane issue (#11+) ships a tool surface *and* a job handler. The skeleton exposes
two symmetric seams so a plane is added by appending to a tuple here and never edits
the entrypoint: `_PLANE_REGISTRARS` (tools, called by :func:`build_app`) and
`_HANDLER_REGISTRARS` (worker job handlers, called by :func:`build_handler_registry`).
Provider-aware registrars receive the injected provider resolver (ADR-0071), while
`jobs.*` register tools but no job handler (they are read/cancel tools, not job kinds).
"""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import ResourceKind
from kdive.jobs.models import HandlerRegistry
from kdive.mcp.auth import build_verifier
from kdive.mcp.middleware import DenialAuditMiddleware
from kdive.mcp.tools.accounting.admin import register as register_accounting_admin
from kdive.mcp.tools.accounting.estimate import register as register_accounting_estimate
from kdive.mcp.tools.accounting.reports import register as register_accounting_reports
from kdive.mcp.tools.accounting.usage import register as register_accounting_usage
from kdive.mcp.tools.catalog import (
    artifacts,
    availability,
    investigations,
    jobs,
    resources,
    shapes,
)
from kdive.mcp.tools.debug import introspect
from kdive.mcp.tools.debug import sessions as debug_tools
from kdive.mcp.tools.lifecycle import allocations
from kdive.mcp.tools.lifecycle import control as control_tools
from kdive.mcp.tools.lifecycle import vmcore as vmcore_tools
from kdive.mcp.tools.lifecycle.runs import registrar as runs_tools
from kdive.mcp.tools.lifecycle.systems import registrar as systems_tools
from kdive.mcp.tools.ops import audit as audit_tools
from kdive.mcp.tools.ops import breakglass as ops_breakglass_tools
from kdive.mcp.tools.ops import inventory as inventory_tools
from kdive.mcp.tools.ops import queue as ops_queue_tools
from kdive.mcp.tools.ops import reconcile as ops_reconcile_tools
from kdive.mcp.tools.ops import tuning as ops_tuning_tools
from kdive.planes import control, runs, systems, vmcore
from kdive.providers.composition import build_provider_resolver
from kdive.providers.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import PROCESS_SECRET_REGISTRY, SecretRegistry

type PlaneRegistrar = Callable[
    [FastMCP, AsyncConnectionPool, ProviderResolver, SecretRegistry], None
]
type HandlerRegistrar = Callable[[HandlerRegistry, ProviderResolver], None]


def _plain(register: Callable[[FastMCP, AsyncConnectionPool], None]) -> PlaneRegistrar:
    def _register(
        app: FastMCP, pool: AsyncConnectionPool, _: ProviderResolver, __: SecretRegistry
    ) -> None:
        register(app, pool)

    return _register


# Tool seam: each plane exposes register(app, pool); provider-aware planes receive the resolver
# and resolve the local-libvirt runtime for their registration-time facets (per-target MCP
# resolution lands with the second provider kind, M1.5 issues 2/4).
_PLANE_REGISTRARS: tuple[PlaneRegistrar, ...] = (
    _plain(jobs.register),
    _plain(resources.register),
    _plain(availability.register),
    _plain(shapes.register),
    _plain(register_accounting_estimate),
    _plain(register_accounting_usage),
    _plain(register_accounting_reports),
    _plain(register_accounting_admin),
    _plain(ops_reconcile_tools.register),
    _plain(allocations.register),
    _plain(ops_breakglass_tools.register),
    lambda app, pool, resolver, registry: systems_tools.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    _plain(investigations.register),
    lambda app, pool, resolver, registry: runs_tools.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    _plain(control_tools.register),
    _plain(artifacts.register),
    lambda app, pool, resolver, registry: vmcore_tools.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    lambda app, pool, resolver, registry: debug_tools.register(
        app,
        pool,
        provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT),
        secret_registry=registry,
    ),
    lambda app, pool, resolver, registry: introspect.register(
        app, pool, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    _plain(ops_queue_tools.register),
    _plain(ops_tuning_tools.register),
    _plain(audit_tools.register),
    _plain(inventory_tools.register),
)

# Handler seam: each concrete worker module exposes register_handlers(registry).
# jobs.* register no JobHandler; the provisioning plane (#16) registers the provision/teardown
# handlers, the build plane (#18) registers the build handler, the control plane (#23)
# registers the power/force_crash handlers, and the retrieve plane (#24) registers the
# capture_vmcore handler (each builds its provider/builder lazily from env — no libvirt/
# toolchain connection at registration). The Connect plane (#20) registers tools only — its
# debug.start_session/end_session are synchronous, so they have no JobKind and no handler.
_HANDLER_REGISTRARS: tuple[HandlerRegistrar, ...] = (
    lambda registry, resolver: systems.register_handlers(
        registry, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    lambda registry, resolver: runs.register_handlers(
        registry, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    lambda registry, resolver: control.register_handlers(
        registry, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
    lambda registry, resolver: vmcore.register_handlers(
        registry, provider_runtime=resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    ),
)


def build_app(
    pool: AsyncConnectionPool,
    *,
    verifier: JWTVerifier | None = None,
    provider_resolver: ProviderResolver | None = None,
    secret_registry: SecretRegistry | None = None,
) -> FastMCP:
    """Construct the FastMCP app and register every plane's tools.

    Args:
        pool: The shared async connection pool tools read through.
        verifier: An injected verifier (tests pass a local-keypair one); when
            ``None``, built from the OIDC env vars via :func:`build_verifier`.
        provider_resolver: Injected per-kind provider resolver passed to provider-aware
            tool registrars; when ``None``, built from the default provider composition.
        secret_registry: App-owned registry shared by secret backends and logging. When
            ``None``, the process-global default is used for tests and CLI helpers.
    """
    app: FastMCP = FastMCP(name="kdive", auth=verifier or build_verifier())
    app.add_middleware(DenialAuditMiddleware(pool))
    resolver = provider_resolver or build_provider_resolver()
    registry = PROCESS_SECRET_REGISTRY if secret_registry is None else secret_registry
    for register in _PLANE_REGISTRARS:
        register(app, pool, resolver, registry)
    return app


def build_handler_registry(*, provider_resolver: ProviderResolver | None = None) -> HandlerRegistry:
    """Build the worker's `HandlerRegistry` from provider-aware handler registrars.

    Args:
        provider_resolver: Injected per-kind provider resolver passed to worker handler
            registrars; when ``None``, built from the default provider composition.
    """
    registry = HandlerRegistry()
    resolver = provider_resolver or build_provider_resolver()
    for register in _HANDLER_REGISTRARS:
        register(registry, resolver)
    return registry
