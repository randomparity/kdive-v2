"""FastMCP application assembly and the two plane registrar seams.

Tool registration and worker-handler registration are both table-driven. A plane adds
tool registrars to ``_PLANE_REGISTRARS`` and long-running job handlers to
``_HANDLER_REGISTRARS``; the entrypoint stays stable. Provider-aware registrars receive
the injected provider resolver (ADR-0071), while read-only/cancel-only tool groups
register no job handler because they do not own a ``JobKind``.
"""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
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
    artifacts,
    availability,
    build_configs,
    fixtures,
    investigations,
    jobs,
    resources,
    shapes,
)
from kdive.mcp.tools.catalog import images as catalog_images
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
from kdive.mcp.tools.ops import images as ops_images_tools
from kdive.mcp.tools.ops import inventory as inventory_tools
from kdive.mcp.tools.ops import queue as ops_queue_tools
from kdive.mcp.tools.ops import reconcile as ops_reconcile_tools
from kdive.mcp.tools.ops import resources as ops_resources_tools
from kdive.mcp.tools.ops import secrets as ops_secrets_tools
from kdive.mcp.tools.ops import tuning as ops_tuning_tools
from kdive.providers.composition import ProviderComposition, build_provider_resolver
from kdive.providers.reaping import InfraReaper
from kdive.providers.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry

type PlaneRegistrar = Callable[
    [FastMCP, AsyncConnectionPool, ProviderResolver, SecretRegistry, InfraReaper], None
]
type HandlerRegistrar = Callable[[HandlerRegistry, ProviderResolver, SecretRegistry], None]


def _plain(register: Callable[[FastMCP, AsyncConnectionPool], None]) -> PlaneRegistrar:
    def _register(
        app: FastMCP,
        pool: AsyncConnectionPool,
        _: ProviderResolver,
        __: SecretRegistry,
        ___: InfraReaper,
    ) -> None:
        register(app, pool)

    return _register


# Tool seam: each plane exposes register(app, pool); provider-aware planes receive the resolver.
_PLANE_REGISTRARS: tuple[PlaneRegistrar, ...] = (
    _plain(jobs.register),
    _plain(resources.register),
    _plain(availability.register),
    _plain(shapes.register),
    _plain(register_accounting_estimate),
    _plain(register_accounting_usage),
    _plain(register_accounting_reports),
    _plain(register_accounting_admin),
    lambda app, pool, resolver, registry, reaper: ops_reconcile_tools.register_with_reaper(
        app,
        pool,
        reaper=reaper,
        upload_store=ops_reconcile_tools.resolve_upload_store(),
        image_store=ops_reconcile_tools.resolve_image_store(),
        dump_volume_reaper=ProviderComposition(
            secret_registry=registry
        ).build_reconciler_dump_volume_reaper(),
    ),
    _plain(ops_resources_tools.register),
    _plain(allocations.register),
    _plain(ops_breakglass_tools.register),
    lambda app, pool, resolver, registry, reaper: systems_tools.register(
        app, pool, resolver=resolver
    ),
    _plain(investigations.register),
    lambda app, pool, resolver, registry, reaper: runs_tools.register(app, pool, resolver=resolver),
    _plain(control_tools.register),
    _plain(artifacts.register),
    _plain(build_configs.register),
    lambda app, pool, resolver, registry, reaper: vmcore_tools.register(
        app, pool, resolver=resolver, secret_registry=registry
    ),
    lambda app, pool, resolver, registry, reaper: debug_tools.register(
        app,
        pool,
        resolver=resolver,
        secret_registry=registry,
    ),
    lambda app, pool, resolver, registry, reaper: introspect.register(app, pool, resolver=resolver),
    _plain(ops_queue_tools.register),
    _plain(ops_tuning_tools.register),
    _plain(audit_tools.register),
    lambda app, pool, resolver, registry, reaper: ops_diagnostics_tools.register(
        app, pool, default_service_factory
    ),
    _plain(inventory_tools.register),
    _plain(fixtures.register),
    _plain(catalog_images.register),
    lambda app, pool, resolver, registry, reaper: ops_images_tools.register_from_env(app, pool),
    lambda app, pool, resolver, registry, reaper: ops_secrets_tools.register(app, pool, registry),
)


def _register_image_build_handler(
    registry: HandlerRegistry, _resolver: ProviderResolver, _secret_registry: SecretRegistry
) -> None:
    """Bind the IMAGE_BUILD handler, preserving setup errors as job failures.

    The handler's build plane is the local-libvirt rootfs plane and the store is the S3 image
    store. A worker with no ``KDIVE_S3_*`` env still binds IMAGE_BUILD so queued jobs fail with
    the original configuration category instead of falling through to ``not_implemented``.
    """
    from kdive.store.objectstore import object_store_from_env

    try:
        store = object_store_from_env()
    except CategorizedError as exc:
        registry.register(JobKind.IMAGE_BUILD, _unconfigured_image_build_handler(exc))
        return
    image_build.register_handlers(
        registry,
        plane_resolver=ProviderComposition(
            secret_registry=_secret_registry
        ).build_rootfs_build_plane_resolver(),
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
    lambda registry, resolver, secret_registry: systems.register_handlers(
        registry, resolver=resolver
    ),
    lambda registry, resolver, secret_registry: runs.register_handlers(
        registry, resolver=resolver, secret_registry=secret_registry
    ),
    lambda registry, resolver, secret_registry: control.register_handlers(
        registry, resolver=resolver
    ),
    lambda registry, resolver, secret_registry: vmcore.register_handlers(
        registry, resolver=resolver
    ),
    _register_image_build_handler,
)


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
    resolver = composition.build_provider_resolver()
    reaper = composition.build_reconciler_reaper()
    for register in _PLANE_REGISTRARS:
        register(app, pool, resolver, secret_registry, reaper)
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
