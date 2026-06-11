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
from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import HTTP_HOST, HTTP_PORT, LOG_LEVEL
from kdive.db.pool import create_pool
from kdive.version import full_version

if TYPE_CHECKING:
    from kdive.health import Heartbeat
    from kdive.observability import Telemetry
    from kdive.providers.resolver import ProviderResolver
    from kdive.security.secrets.secret_registry import SecretRegistry

_RUNNABLE = frozenset({"server", "worker", "reconciler", "migrate"})

# Server /livez heartbeat: ticked at this cadence, stale after the larger bound. The
# server's "loop" is its asyncio event loop, so a stale tick means the loop is wedged.
_HEARTBEAT_TICK_SECONDS = 1.0
_HEARTBEAT_STALE_SECONDS = 10.0

_log = logging.getLogger(__name__)


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
        action="version",
        version=f"kdive {full_version()}",
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
    aux_host, aux_port = resolve_health_bind()
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


async def _run_worker(secret_registry: SecretRegistry) -> None:
    from kdive.jobs.worker import Worker
    from kdive.mcp.app import build_handler_registry

    pool = create_pool(min_size=2, max_size=4)
    await pool.open()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    try:
        worker = Worker(
            pool,
            build_handler_registry(secret_registry=secret_registry),
            worker_id=worker_id,
            secret_registry=secret_registry,
        )
        await worker.run(stop)
    finally:
        secret_registry.clear()
        await pool.close()


async def _run_reconciler(secret_registry: SecretRegistry) -> None:
    from kdive.domain.errors import CategorizedError
    from kdive.providers.composition import ProviderComposition
    from kdive.reconciler.loop import Reconciler
    from kdive.store.objectstore import object_store_from_env

    pool = create_pool(min_size=1)
    await pool.open()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        upload_store = object_store_from_env()
    except CategorizedError:
        upload_store = None  # no S3 env: the upload reaper stays off, like NullReaper
    provider_composition = ProviderComposition()
    await _register_provider_resources(pool, provider_composition.build_provider_resolver())
    try:
        reconciler = Reconciler(
            pool,
            provider_composition.build_reconciler_reaper(),
            upload_store=upload_store,
            resetter=provider_composition.build_reconciler_transport_resetter(),
        )
        await reconciler.run(stop)
    finally:
        secret_registry.clear()
        await pool.close()


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
        asyncio.run(_run_worker(secret_registry))
    elif args.command == "reconciler":
        asyncio.run(_run_reconciler(secret_registry))
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


if __name__ == "__main__":
    main()
