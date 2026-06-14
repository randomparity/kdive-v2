"""FastMCP application assembly and the two plane registrar seams.

Tool registration and worker-handler registration are both table-driven. A plane adds
tool registrars to ``_PLANE_REGISTRARS`` and long-running job handlers to
``_HANDLER_REGISTRARS``; the entrypoint stays stable. Provider-aware registrars receive
the assembled provider/env ports (ADR-0071), while read-only/cancel-only tool groups register
no job handler because they do not own a ``JobKind``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools import Tool
from opentelemetry import metrics, trace
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.service import default_service_factory
from kdive.domain.errors import CategorizedError
from kdive.domain.models import Job, JobKind
from kdive.jobs.handlers import control, image_build, runs, systems, vmcore
from kdive.jobs.models import HandlerRegistry, JobHandler
from kdive.mcp.auth import build_verifier
from kdive.mcp.middleware import DenialAuditMiddleware, TelemetryMiddleware
from kdive.mcp.tools.accounting.admin import register as register_accounting_admin
from kdive.mcp.tools.accounting.estimate import register as register_accounting_estimate
from kdive.mcp.tools.accounting.reports import register as register_accounting_reports
from kdive.mcp.tools.accounting.usage import register as register_accounting_usage
from kdive.mcp.tools.catalog import (
    availability,
    build_configs,
    fixtures,
    investigations,
    jobs,
    resources,
    shapes,
)
from kdive.mcp.tools.catalog import images as catalog_images
from kdive.mcp.tools.catalog.artifacts import registrar as artifacts_tools
from kdive.mcp.tools.debug import introspect
from kdive.mcp.tools.debug import sessions as debug_tools
from kdive.mcp.tools.lifecycle import allocations
from kdive.mcp.tools.lifecycle import control as control_tools
from kdive.mcp.tools.lifecycle import vmcore as vmcore_tools
from kdive.mcp.tools.lifecycle.runs import registrar as runs_tools
from kdive.mcp.tools.lifecycle.systems import registrar as systems_tools
from kdive.mcp.tools.ops import audit as audit_tools
from kdive.mcp.tools.ops import breakglass as ops_breakglass_tools
from kdive.mcp.tools.ops import diagnostics as ops_diagnostics_tools
from kdive.mcp.tools.ops import inventory as inventory_tools
from kdive.mcp.tools.ops import queue as ops_queue_tools
from kdive.mcp.tools.ops import reconcile as ops_reconcile_tools
from kdive.mcp.tools.ops import reconcile_systems as ops_reconcile_systems_tools
from kdive.mcp.tools.ops import resources as ops_resources_tools
from kdive.mcp.tools.ops import secrets as ops_secrets_tools
from kdive.mcp.tools.ops import tuning as ops_tuning_tools
from kdive.mcp.tools.ops.build_hosts import registrar as ops_build_hosts_tools
from kdive.mcp.tools.ops.images import registrar as ops_images_tools
from kdive.providers.composition import ProviderComposition, build_provider_resolver
from kdive.providers.reaping import BuildVmReaper, DumpVolumeReaper, InfraReaper
from kdive.providers.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry


@dataclass(frozen=True, slots=True)
class AppAssembly:
    """Provider/env ports assembled once for MCP tool registration."""

    resolver: ProviderResolver
    secret_registry: SecretRegistry
    reaper: InfraReaper
    dump_volume_reaper: DumpVolumeReaper
    build_vm_reaper: BuildVmReaper


type PlaneRegistrar = Callable[[FastMCP, AsyncConnectionPool, AppAssembly], None]
type HandlerRegistrar = Callable[[HandlerRegistry, ProviderResolver, SecretRegistry], None]


def _pool_only_plane_registrar(
    register: Callable[[FastMCP, AsyncConnectionPool], None],
) -> PlaneRegistrar:
    def _register(
        app: FastMCP,
        pool: AsyncConnectionPool,
        _: AppAssembly,
    ) -> None:
        register(app, pool)

    return _register


def _register_reconcile_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    ops_reconcile_tools.register_with_reaper(
        app,
        pool,
        reaper=assembly.reaper,
        upload_store=ops_reconcile_tools.resolve_upload_store(),
        image_store=ops_reconcile_tools.resolve_image_store(),
        dump_volume_reaper=assembly.dump_volume_reaper,
        build_vm_reaper=assembly.build_vm_reaper,
    )


def _register_reconcile_systems_tools(
    app: FastMCP, pool: AsyncConnectionPool, _assembly: AppAssembly
) -> None:
    ops_reconcile_systems_tools.register(
        app, pool, image_store=ops_reconcile_tools.resolve_image_store()
    )


def _register_systems_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    systems_tools.register(app, pool, resolver=assembly.resolver)


def _register_runs_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    runs_tools.register(app, pool, resolver=assembly.resolver)


def _register_control_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    control_tools.register(app, pool, resolver=assembly.resolver)


def _register_artifact_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    artifacts_tools.register(app, pool, resolver=assembly.resolver)


def _register_vmcore_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    vmcore_tools.register(
        app, pool, resolver=assembly.resolver, secret_registry=assembly.secret_registry
    )


def _register_debug_tools(app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly) -> None:
    debug_tools.register(
        app,
        pool,
        resolver=assembly.resolver,
        secret_registry=assembly.secret_registry,
    )


def _register_introspection_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    introspect.register(app, pool, resolver=assembly.resolver)


def _register_diagnostics_tools(
    app: FastMCP, pool: AsyncConnectionPool, _assembly: AppAssembly
) -> None:
    ops_diagnostics_tools.register(app, pool, default_service_factory)


def _register_ops_build_hosts_tools(
    app: FastMCP, pool: AsyncConnectionPool, _assembly: AppAssembly
) -> None:
    ops_build_hosts_tools.register(app, pool)


def _register_ops_images_tools(
    app: FastMCP, pool: AsyncConnectionPool, _assembly: AppAssembly
) -> None:
    ops_images_tools.register_from_env(app, pool)


def _register_ops_secrets_tools(
    app: FastMCP, pool: AsyncConnectionPool, assembly: AppAssembly
) -> None:
    ops_secrets_tools.register(app, pool, assembly.secret_registry)


def _register_system_handlers(
    registry: HandlerRegistry, resolver: ProviderResolver, _secret_registry: SecretRegistry
) -> None:
    systems.register_handlers(registry, resolver=resolver)


def _register_run_handlers(
    registry: HandlerRegistry, resolver: ProviderResolver, secret_registry: SecretRegistry
) -> None:
    runs.register_handlers(registry, resolver=resolver, secret_registry=secret_registry)


def _register_control_handlers(
    registry: HandlerRegistry, resolver: ProviderResolver, _secret_registry: SecretRegistry
) -> None:
    control.register_handlers(registry, resolver=resolver)


def _register_vmcore_handlers(
    registry: HandlerRegistry, resolver: ProviderResolver, _secret_registry: SecretRegistry
) -> None:
    vmcore.register_handlers(registry, resolver=resolver)


# Tool seam: each plane exposes register(app, pool); provider-aware planes receive AppAssembly.
_PLANE_REGISTRARS: tuple[PlaneRegistrar, ...] = (
    _pool_only_plane_registrar(jobs.register),
    _pool_only_plane_registrar(resources.register),
    _pool_only_plane_registrar(availability.register),
    _pool_only_plane_registrar(shapes.register),
    _pool_only_plane_registrar(register_accounting_estimate),
    _pool_only_plane_registrar(register_accounting_usage),
    _pool_only_plane_registrar(register_accounting_reports),
    _pool_only_plane_registrar(register_accounting_admin),
    _register_reconcile_tools,
    _register_reconcile_systems_tools,
    _pool_only_plane_registrar(ops_resources_tools.register),
    _pool_only_plane_registrar(allocations.register),
    _pool_only_plane_registrar(ops_breakglass_tools.register),
    _register_systems_tools,
    _pool_only_plane_registrar(investigations.register),
    _register_runs_tools,
    _register_control_tools,
    _register_artifact_tools,
    _pool_only_plane_registrar(build_configs.register),
    _register_vmcore_tools,
    _register_debug_tools,
    _register_introspection_tools,
    _pool_only_plane_registrar(ops_queue_tools.register),
    _pool_only_plane_registrar(ops_tuning_tools.register),
    _pool_only_plane_registrar(audit_tools.register),
    _register_diagnostics_tools,
    _pool_only_plane_registrar(inventory_tools.register),
    _pool_only_plane_registrar(fixtures.register),
    _pool_only_plane_registrar(catalog_images.register),
    _register_ops_build_hosts_tools,
    _register_ops_images_tools,
    _register_ops_secrets_tools,
)


def _register_image_build_handler(
    registry: HandlerRegistry, resolver: ProviderResolver, _secret_registry: SecretRegistry
) -> None:
    """Bind the IMAGE_BUILD handler, preserving setup errors as job failures.

    The handler resolves the provider's rootfs build plane through ``ProviderResolver``; the S3
    image store is still assembled once at worker registration. A worker with no ``KDIVE_S3_*``
    env still binds IMAGE_BUILD so queued jobs fail with the original configuration category
    instead of falling through to ``not_implemented``.
    """
    from kdive.store.objectstore import object_store_from_env

    try:
        store = object_store_from_env()
    except CategorizedError as exc:
        registry.register(JobKind.IMAGE_BUILD, _unconfigured_image_build_handler(exc))
        return
    image_build.register_handlers(
        registry,
        provider_resolver=resolver,
        store=store,
    )


def _unconfigured_image_build_handler(
    error: CategorizedError,
) -> JobHandler:
    async def _handler(_conn: AsyncConnection, _job: Job) -> str | None:
        raise CategorizedError(str(error), category=error.category, details=error.details)

    return _handler


# Handler seam: worker modules expose register_handlers(registry). Long-running lifecycle,
# build, control, and retrieval operations register JobKind handlers here; synchronous tools
# register only in _PLANE_REGISTRARS. Handler construction receives the provider resolver and
# redaction registry without opening provider or toolchain connections at registration time.
_HANDLER_REGISTRARS: tuple[HandlerRegistrar, ...] = (
    _register_system_handlers,
    _register_run_handlers,
    _register_control_handlers,
    _register_vmcore_handlers,
    _register_image_build_handler,
)


# A flat, non-recursive output schema advertised for every tool (ADR-0113). Every tool returns
# the self-referential `ToolResponse` (`items: list[ToolResponse]` + recursive `JsonValue` data),
# so FastMCP would auto-derive a recursive `$ref` schema that the FastMCP 3.4.0 client cannot
# build a validator for — it logs a per-call parse error and nulls `CallToolResult.data`.
# Advertising a flat object removes the recursion while keeping the `structured_content` wire
# payload unchanged (no `x-fastmcp-wrap-result` key). Typed `dict[str, Any]` to match FastMCP's
# `Tool.output_schema` and because a JSON schema nests non-str values.
ENVELOPE_OUTPUT_SCHEMA: dict[str, Any] = {"type": "object"}


def _advertise_flat_output_schema(app: FastMCP) -> int:
    """Override every registered tool's advertised `outputSchema` with the flat envelope schema.

    Mutates the *live* registered `Tool` instances (the `Tool`-typed values in the local
    provider's component store); `app.list_tools()` returns copies whose mutation would not change
    what the server advertises. Raises if no tools are found: `build_app` always registers a
    non-empty surface, so a zero count means the FastMCP registry accessor changed under us and
    the app must not silently fall back to advertising the recursive schema (ADR-0113).

    Returns the number of tools swept.
    """
    swept = 0
    for component in app.local_provider._components.values():
        if isinstance(component, Tool):
            component.output_schema = dict(ENVELOPE_OUTPUT_SCHEMA)
            swept += 1
    if swept == 0:
        raise RuntimeError(
            "no tools found to advertise a flat outputSchema for; the FastMCP registry accessor "
            "(app.local_provider._components) may have changed (ADR-0113)"
        )
    return swept


def build_app(
    pool: AsyncConnectionPool,
    *,
    verifier: JWTVerifier | None = None,
    provider_composition: ProviderComposition | None = None,
    secret_registry: SecretRegistry,
) -> FastMCP:
    """Construct the FastMCP app and register every plane's tools.

    Args:
        pool: The shared async connection pool tools read through.
        verifier: An injected verifier (tests pass a local-keypair one); when
            ``None``, built from the OIDC env vars via :func:`build_verifier`.
        provider_composition: Provider assembly owner used when the app constructs its own
            resolver/reaper pair.
        secret_registry: App-owned registry shared by secret backends and logging.
    """
    app: FastMCP = FastMCP(name="kdive", auth=verifier or build_verifier())
    # Telemetry runs outermost (added first) so its span/RED metrics wrap the whole
    # dispatch, including a denial mapped by DenialAuditMiddleware (ADR-0090 §5). Both
    # use the process-global OTel providers, which no-op until init_telemetry runs.
    app.add_middleware(
        TelemetryMiddleware(
            tracer=trace.get_tracer("kdive.mcp"), meter=metrics.get_meter("kdive.mcp")
        )
    )
    app.add_middleware(DenialAuditMiddleware(pool))
    composition = provider_composition or ProviderComposition(secret_registry=secret_registry)
    assembly = AppAssembly(
        resolver=composition.build_provider_resolver(),
        secret_registry=composition.secret_registry,
        reaper=composition.build_reconciler_reaper(),
        dump_volume_reaper=composition.build_reconciler_dump_volume_reaper(),
        build_vm_reaper=composition.build_reconciler_build_vm_reaper(),
    )
    for register in _PLANE_REGISTRARS:
        register(app, pool, assembly)
    _advertise_flat_output_schema(app)
    return app


def build_handler_registry(
    *, provider_resolver: ProviderResolver | None = None, secret_registry: SecretRegistry
) -> HandlerRegistry:
    """Build the worker's `HandlerRegistry` from provider-aware handler registrars.

    Args:
        provider_resolver: Injected per-kind provider resolver passed to worker handler
            registrars; when ``None``, built from the default provider composition.
        secret_registry: Worker-owned registry shared by redaction boundaries and logging.
    """
    registry = HandlerRegistry()
    resolver = provider_resolver or build_provider_resolver(secret_registry=secret_registry)
    for register in _HANDLER_REGISTRARS:
        register(registry, resolver, secret_registry)
    return registry
