"""Process entrypoints: `python -m kdive server|worker|reconciler`.

`server` runs the FastMCP streamable-HTTP app; `worker` runs the job-queue worker
loop; `reconciler` runs the drift-repair loop (ADR-0021). All three configure the
structured logger first (ADR-0014).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import HTTP_HOST, HTTP_PORT, LOG_LEVEL
from kdive.db.pool import create_pool
from kdive.images.rootfs_command import add_build_rootfs_parser, run_build_rootfs
from kdive.reconciler.console_assembly import build_console_hosting, start_console_hosting
from kdive.version import full_version

if TYPE_CHECKING:
    from kdive.health import HealthProbe, Heartbeat
    from kdive.observability import Telemetry
    from kdive.providers.resolver import ProviderResolver
    from kdive.security.secrets.secret_registry import SecretRegistry

_RUNNABLE = frozenset({"server", "worker", "reconciler", "migrate"})

# Server /livez heartbeat: ticked at this cadence, stale after the larger bound. The
# server's "loop" is its asyncio event loop, so a stale tick means the loop is wedged.
# The worker reuses the same stale bound: its poll interval is ~1s, so a healthy claim
# loop re-ticks well inside 10s, while a wedged loop (no poll pass) goes stale.
_HEARTBEAT_TICK_SECONDS = 1.0
_HEARTBEAT_STALE_SECONDS = 10.0
# The reconciler ticks once per pass on a 30s interval (DEFAULT_INTERVAL); the stale bound
# is sized above two intervals so a single slow-but-progressing pass never reads not-live,
# while a wedged loop that misses two scheduled passes does.
_RECONCILER_HEARTBEAT_STALE_SECONDS = 90.0

_log = logging.getLogger(__name__)


class _VersionAction(argparse.Action):
    """Print the full version only when ``--version`` is selected."""

    def __init__(self, option_strings: list[str], dest: str = argparse.SUPPRESS) -> None:
        super().__init__(option_strings=option_strings, dest=dest, nargs=0)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del namespace, values, option_string
        parser.exit(message=f"kdive {full_version()}\n")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with the `server`/`worker`/`reconciler` subcommands."""
    parser = argparse.ArgumentParser(prog="kdive")
    parser.add_argument(
        "--log-level",
        default=None,
        help="structured-logging level (default: KDIVE_LOG_LEVEL, else INFO)",
    )
    parser.add_argument(
        "--version",
        action=_VersionAction,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("server", help="run the MCP streamable-HTTP server")
    sub.add_parser("worker", help="run the job-queue worker loop")
    sub.add_parser("reconciler", help="run the drift-repair reconciler loop")
    sub.add_parser("migrate", help="apply database migrations")
    fixtures = sub.add_parser("install-fixtures", help="install default fixture catalog")
    fixtures.add_argument("--dest", default="/etc/kdive/fixtures/local-libvirt")
    fixtures.add_argument("--force", action="store_true", help="overwrite existing files")
    seed = sub.add_parser("seed-demo", help="seed a project for local agent demos")
    seed.add_argument("--project", default="demo")
    seed.add_argument("--limit-kcu", default="1000000")
    seed.add_argument("--max-concurrent-allocations", type=int, default=4)
    seed.add_argument("--max-concurrent-systems", type=int, default=4)
    add_build_rootfs_parser(sub)
    return parser


async def _run_server(
    host: str, port: int, secret_registry: SecretRegistry, telemetry: Telemetry
) -> None:
    from kdive.health import HealthProbe, Heartbeat, build_aux_app, serve_aux
    from kdive.health.aux_bind import resolve_health_bind
    from kdive.health.server_checks import build_server_checks
    from kdive.mcp.app import build_app
    from kdive.server_health import build_oidc_ping, build_postgres_ping
    from kdive.store.objectstore import object_store_from_env

    pool = create_pool()
    await pool.open()
    heartbeat = Heartbeat(stale_after=_HEARTBEAT_STALE_SECONDS)
    probe = HealthProbe(
        checks=build_server_checks(
            postgres_ping=build_postgres_ping(pool),
            object_store_factory=object_store_from_env,
            oidc_ping=build_oidc_ping(),
        )
    )
    aux_host, aux_port = resolve_health_bind("server")
    aux_app = build_aux_app(heartbeat=heartbeat, probe=probe, metric_reader=telemetry.scrape_reader)
    app = build_app(pool, secret_registry=secret_registry)
    aux_task = asyncio.create_task(serve_aux(aux_app, host=aux_host, port=aux_port))
    ticker = asyncio.create_task(_tick_heartbeat(heartbeat))
    try:
        await app.run_async(transport="http", host=host, port=port)
    finally:
        await _cancel(ticker, aux_task)
        secret_registry.clear()
        await pool.close()


async def _cancel(*tasks: asyncio.Task[None]) -> None:
    """Cancel and await aux background tasks before the pool/registry are torn down.

    Awaiting (suppressing ``CancelledError``) drains an in-flight ``/readyz`` — which
    borrows a pool connection — so the pool never closes underneath it, and avoids a
    "Task was destroyed but it is pending" warning on exit.
    """
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _tick_heartbeat(heartbeat: Heartbeat) -> None:
    """Bump the server heartbeat each tick so /livez tracks event-loop responsiveness."""
    while True:
        heartbeat.tick()
        await asyncio.sleep(_HEARTBEAT_TICK_SECONDS)


def _install_stop() -> asyncio.Event:
    """Build a stop event set on SIGINT/SIGTERM (the worker/reconciler shutdown signal)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    return stop


def _readiness(probe: HealthProbe) -> Callable[[], Awaitable[bool]]:
    """Adapt a :class:`HealthProbe` into the worker's boolean readiness gate (ADR-0090 §5).

    The worker pauses dequeuing new jobs while not-ready; the probe's healthy-cached /
    failure-immediate asymmetry means a recovered backend resumes claiming at once.
    """

    async def ready() -> bool:
        return (await probe.check()).ready

    return ready


async def _run_worker(secret_registry: SecretRegistry, telemetry: Telemetry) -> None:
    from kdive.health import Heartbeat, build_aux_app, serve_aux
    from kdive.health.aux_bind import resolve_health_bind
    from kdive.jobs.worker import Worker
    from kdive.jobs.worker_telemetry import WorkerTelemetry
    from kdive.mcp.app import build_handler_registry
    from kdive.server_health import build_postgres_ping
    from kdive.store.objectstore import object_store_from_env
    from kdive.worker_health import build_worker_probe

    pool = create_pool(min_size=2, max_size=4)
    await pool.open()
    stop = _install_stop()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    heartbeat = Heartbeat(stale_after=_HEARTBEAT_STALE_SECONDS)
    probe = build_worker_probe(
        postgres_ping=build_postgres_ping(pool), object_store_factory=object_store_from_env
    )
    aux_host, aux_port = resolve_health_bind("worker")
    aux_app = build_aux_app(heartbeat=heartbeat, probe=probe, metric_reader=telemetry.scrape_reader)
    aux_task = asyncio.create_task(serve_aux(aux_app, host=aux_host, port=aux_port))
    try:
        worker = Worker(
            pool,
            build_handler_registry(secret_registry=secret_registry),
            worker_id=worker_id,
            secret_registry=secret_registry,
            heartbeat=heartbeat,
            readiness=_readiness(probe),
            telemetry=WorkerTelemetry(
                tracer=telemetry.tracer_provider.get_tracer("kdive.worker"),
                meter=telemetry.meter_provider.get_meter("kdive.worker"),
            ),
        )
        await worker.run(stop)
    finally:
        await _cancel(aux_task)
        secret_registry.clear()
        await pool.close()


async def _run_reconciler(secret_registry: SecretRegistry, telemetry: Telemetry) -> None:
    from kdive.domain.errors import CategorizedError
    from kdive.health import Heartbeat, build_aux_app, serve_aux
    from kdive.health.aux_bind import resolve_health_bind
    from kdive.providers.composition import ProviderComposition
    from kdive.reconciler.loop import Reconciler
    from kdive.reconciler.loop_telemetry import ReconcilerTelemetry
    from kdive.server_health import build_postgres_ping
    from kdive.store.objectstore import object_store_from_env
    from kdive.worker_health import build_worker_probe

    pool = create_pool(min_size=1)
    await pool.open()
    stop = _install_stop()
    try:
        upload_store = object_store_from_env()
    except CategorizedError:
        upload_store = None  # no S3 env: the upload reaper stays off, like NullReaper
    heartbeat = Heartbeat(stale_after=_RECONCILER_HEARTBEAT_STALE_SECONDS)
    probe = _reconciler_probe(pool, build_postgres_ping, build_worker_probe, object_store_from_env)
    aux_host, aux_port = resolve_health_bind("reconciler")
    aux_app = build_aux_app(heartbeat=heartbeat, probe=probe, metric_reader=telemetry.scrape_reader)
    aux_task = asyncio.create_task(serve_aux(aux_app, host=aux_host, port=aux_port))
    provider_composition = ProviderComposition()
    await _register_provider_resources(pool, provider_composition.build_provider_resolver())
    console_hosting = await build_console_hosting(secret_registry)
    try:
        reconciler = Reconciler(
            pool,
            provider_composition.build_reconciler_reaper(),
            upload_store=upload_store,
            image_store=upload_store,
            console_registry=console_hosting.registry if console_hosting else None,
            resetter=provider_composition.build_reconciler_transport_resetter(),
            dump_volume_reaper=provider_composition.build_reconciler_dump_volume_reaper(),
            heartbeat=heartbeat,
            telemetry=ReconcilerTelemetry(
                tracer=telemetry.tracer_provider.get_tracer("kdive.reconciler"),
                meter=telemetry.meter_provider.get_meter("kdive.reconciler"),
            ),
        )
        hosting_task = start_console_hosting(console_hosting, stop)
        try:
            await reconciler.run(stop)
        finally:
            await _cancel(*([hosting_task] if hosting_task else []))
            if console_hosting is not None:
                await console_hosting.close()
    finally:
        await _cancel(aux_task)
        secret_registry.clear()
        await pool.close()


def _reconciler_probe(
    pool: AsyncConnectionPool,
    build_postgres_ping: Callable[[AsyncConnectionPool], Callable[[], Awaitable[None]]],
    build_worker_probe: Callable[..., HealthProbe],
    object_store_factory: Callable[[], object],
) -> HealthProbe:
    """Build the reconciler readiness probe (PG + MinIO, no OIDC — same set as the worker)."""
    return build_worker_probe(
        postgres_ping=build_postgres_ping(pool), object_store_factory=object_store_factory
    )


async def _register_provider_resources(
    pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    """Best-effort provider discovery registration so allocations.request has a Resource.

    A provider that can't be reached/registered must not crash the reconciler — the other
    repairs still run, and the next startup retries; the failure is logged.
    """
    try:
        await resolver.register_all_discovery(pool)
    except Exception:  # noqa: BLE001 - registration failure must not crash the reconciler
        _log.warning("reconciler: provider discovery registration failed at startup", exc_info=True)


def main(argv: list[str] | None = None) -> None:
    """Parse arguments, configure logging, and dispatch to the chosen subcommand."""
    args = build_parser().parse_args(argv)
    from kdive.observability import bootstrap_stdout_floor, init_telemetry
    from kdive.security.secrets.secret_registry import SecretRegistry

    # Snapshot the environment before any setting is read, including the logging
    # bootstrap (ADR-0087 decision 4): config.load() must precede the first config.get().
    config.load()
    level = args.log_level or config.require(LOG_LEVEL)
    secret_registry = SecretRegistry()
    # Bootstrap-ordering invariant (ADR-0090 §1): the stdlib stdout JSON floor is the
    # first startup step — before the OTel providers, config validation, or any backend
    # client — so early-startup records (config-validation failures, the most common
    # first-run fault) are never lost to an unconfigured root logger.
    bootstrap_stdout_floor(level, secret_registry=secret_registry)
    telemetry = None
    if args.command in _RUNNABLE:
        config.validate(args.command)
        # The config is validated on the stdout floor first; only then is the OTel
        # pipeline (which may construct an OTLP client) built and the floor handed over.
        telemetry = init_telemetry(args.command, secret_registry=secret_registry, level=level)
    _log.info("starting kdive %s (%s)", full_version(), args.command)
    if args.command == "server":
        assert telemetry is not None  # server is in _RUNNABLE, so telemetry was built
        host = config.require(HTTP_HOST)
        port = config.require(HTTP_PORT)
        asyncio.run(_run_server(host, port, secret_registry, telemetry))
    elif args.command == "worker":
        assert telemetry is not None  # worker is in _RUNNABLE, so telemetry was built
        asyncio.run(_run_worker(secret_registry, telemetry))
    elif args.command == "reconciler":
        assert telemetry is not None  # reconciler is in _RUNNABLE, so telemetry was built
        asyncio.run(_run_reconciler(secret_registry, telemetry))
    elif args.command == "migrate":
        from kdive.admin.bootstrap import migrate

        migrate()
    elif args.command == "install-fixtures":
        from pathlib import Path

        from kdive.admin.bootstrap import install_fixtures

        install_fixtures(Path(args.dest), force=args.force)
    elif args.command == "seed-demo":
        from decimal import Decimal

        from kdive.admin.bootstrap import seed_demo

        asyncio.run(
            seed_demo(
                project=args.project,
                limit_kcu=Decimal(args.limit_kcu),
                max_concurrent_allocations=args.max_concurrent_allocations,
                max_concurrent_systems=args.max_concurrent_systems,
            )
        )
    elif args.command == "build-rootfs":
        run_build_rootfs(args)


if __name__ == "__main__":
    main()
