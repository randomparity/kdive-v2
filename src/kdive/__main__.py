"""CLI entrypoints for KDIVE processes and operator commands.

The long-running processes are `python -m kdive {server|worker|reconciler}`:
`server` runs the FastMCP streamable-HTTP app, `worker` runs the job-queue worker
loop, and `reconciler` runs the drift-repair loop (ADR-0021). One-shot operator
commands share the same parser: `migrate`, `install-fixtures`, `seed-demo`, and
`build-rootfs`. Every command configures the structured logger first (ADR-0014).
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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import (
    HTTP_HOST,
    HTTP_PORT,
    LOG_LEVEL,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_REGION,
)
from kdive.db.pool import create_pool
from kdive.domain.errors import CategorizedError
from kdive.images.rootfs_command import add_build_rootfs_parser, run_build_rootfs
from kdive.reconciler.console_assembly import start_console_hosting
from kdive.version import full_version

if TYPE_CHECKING:
    from kdive.health import HealthProbe, Heartbeat
    from kdive.observability import Telemetry
    from kdive.providers.resolver import ProviderResolver
    from kdive.security.secrets.secret_registry import SecretRegistry
    from kdive.store.objectstore import ObjectStore

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
_PROVIDER_DISCOVERY_TIMEOUT_SECONDS = 30.0

_log = logging.getLogger(__name__)
_S3_OPTIONAL_ENV_NAMES = frozenset({S3_ENDPOINT_URL.name, S3_BUCKET.name, S3_REGION.name})


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
        print(f"kdive {full_version()}")
        parser.exit()


type _CommandHandler = Callable[[argparse.Namespace, SecretRegistry, Telemetry | None], None]
type _ArgumentAdder = Callable[[argparse.ArgumentParser], None]
type _CommandRegistrar = Callable[[Any], None]


@dataclass(frozen=True, slots=True)
class _Command:
    name: str
    help: str
    handler: _CommandHandler
    runnable: bool = False
    add_arguments: _ArgumentAdder | None = None
    custom_register: _CommandRegistrar | None = None

    def register(self, subparsers: Any) -> None:
        if self.custom_register is not None:
            self.custom_register(subparsers)
            return
        parser = subparsers.add_parser(self.name, help=self.help)
        if self.add_arguments is not None:
            self.add_arguments(parser)


def _require_telemetry(command: str, telemetry: Telemetry | None) -> Telemetry:
    if telemetry is None:
        raise RuntimeError(f"{command} command requires telemetry bootstrap")
    return telemetry


def _add_install_fixtures_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dest", default="/etc/kdive/fixtures/local-libvirt")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")


def _add_seed_demo_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default="demo")
    parser.add_argument("--limit-kcu", default="1000000")
    parser.add_argument("--max-concurrent-allocations", type=int, default=4)
    parser.add_argument("--max-concurrent-systems", type=int, default=4)


def _handle_server(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del args
    initialized = _require_telemetry("server", telemetry)
    host = config.require(HTTP_HOST)
    port = config.require(HTTP_PORT)
    asyncio.run(_run_server(host, port, secret_registry, initialized))


def _handle_worker(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del args
    asyncio.run(_run_worker(secret_registry, _require_telemetry("worker", telemetry)))


def _handle_reconciler(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del args
    asyncio.run(_run_reconciler(secret_registry, _require_telemetry("reconciler", telemetry)))


def _handle_migrate(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del args, secret_registry, telemetry
    from kdive.admin.bootstrap import migrate

    migrate()


def _handle_install_fixtures(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del secret_registry, telemetry
    from pathlib import Path

    from kdive.admin.bootstrap import install_fixtures

    install_fixtures(Path(args.dest), force=args.force)


def _handle_seed_demo(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del secret_registry, telemetry
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


def _handle_build_rootfs(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del secret_registry, telemetry
    run_build_rootfs(args)


def _add_reconcile_systems_arguments(parser: argparse.ArgumentParser) -> None:
    from pathlib import Path

    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="path to systems.toml (default: KDIVE_SYSTEMS_TOML, then ./systems.toml)",
    )


def _handle_reconcile_systems(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del secret_registry, telemetry
    from kdive.inventory.reconcile_cli import reconcile_systems
    from kdive.store.objectstore import object_store_from_env

    store = _optional_reconciler_object_store(object_store_from_env)
    if store is None:
        raise SystemExit(
            "reconcile-systems requires an object store; set KDIVE_S3_ENDPOINT_URL / "
            "KDIVE_S3_BUCKET / KDIVE_S3_REGION (the pass HEADs s3 image objects)."
        )
    pool = create_pool(min_size=1)

    async def _run() -> int:
        await pool.open()
        try:
            return await reconcile_systems(args.path, pool=pool, store=store)
        finally:
            await pool.close()

    raise SystemExit(asyncio.run(_run()))


_COMMANDS: tuple[_Command, ...] = (
    _Command("server", "run the MCP streamable-HTTP server", _handle_server, runnable=True),
    _Command("worker", "run the job-queue worker loop", _handle_worker, runnable=True),
    _Command(
        "reconciler", "run the drift-repair reconciler loop", _handle_reconciler, runnable=True
    ),
    _Command("migrate", "apply database migrations", _handle_migrate, runnable=True),
    _Command(
        "install-fixtures",
        "install default fixture catalog",
        _handle_install_fixtures,
        add_arguments=_add_install_fixtures_arguments,
    ),
    _Command(
        "seed-demo",
        "seed a project for local agent demos",
        _handle_seed_demo,
        add_arguments=_add_seed_demo_arguments,
    ),
    _Command(
        "build-rootfs",
        "build the default local-libvirt rootfs image",
        _handle_build_rootfs,
        custom_register=add_build_rootfs_parser,
    ),
    _Command(
        "reconcile-systems",
        "reconcile systems.toml into the catalog once",
        _handle_reconcile_systems,
        add_arguments=_add_reconcile_systems_arguments,
    ),
)
_COMMAND_BY_NAME = {command.name: command for command in _COMMANDS}
_RUNNABLE = frozenset(command.name for command in _COMMANDS if command.runnable)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser for process and operator subcommands."""
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
    for command in _COMMANDS:
        command.register(sub)
    return parser


async def _run_server(
    host: str, port: int, secret_registry: SecretRegistry, telemetry: Telemetry
) -> None:
    from kdive.health import HealthProbe, Heartbeat, build_aux_app, serve_aux
    from kdive.health.aux_bind import resolve_health_bind
    from kdive.health.server_checks import build_server_checks
    from kdive.mcp.app import build_app
    from kdive.process_health.server import build_oidc_ping, build_postgres_ping
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
    from kdive.jobs.worker import Worker, WorkerConfig
    from kdive.jobs.worker_telemetry import WorkerTelemetry
    from kdive.mcp.app import build_handler_registry
    from kdive.process_health.server import build_postgres_ping
    from kdive.process_health.worker import build_worker_probe
    from kdive.store.objectstore import object_store_from_env

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
            config=WorkerConfig(
                heartbeat=heartbeat,
                readiness=_readiness(probe),
                telemetry=WorkerTelemetry(
                    tracer=telemetry.tracer_provider.get_tracer("kdive.worker"),
                    meter=telemetry.meter_provider.get_meter("kdive.worker"),
                ),
            ),
        )
        await worker.run(stop)
    finally:
        await _cancel(aux_task)
        secret_registry.clear()
        await pool.close()


async def _run_reconciler(secret_registry: SecretRegistry, telemetry: Telemetry) -> None:
    from kdive.health import Heartbeat, build_aux_app, serve_aux
    from kdive.health.aux_bind import resolve_health_bind
    from kdive.process_health.server import build_postgres_ping
    from kdive.process_health.worker import build_worker_probe
    from kdive.providers.composition import ProviderComposition
    from kdive.reconciler.loop import ReconcileConfig, Reconciler
    from kdive.reconciler.loop_telemetry import ReconcilerTelemetry
    from kdive.store.objectstore import object_store_from_env

    pool = create_pool(min_size=1)
    await pool.open()
    stop = _install_stop()
    upload_store = _optional_reconciler_object_store(object_store_from_env)
    heartbeat = Heartbeat(stale_after=_RECONCILER_HEARTBEAT_STALE_SECONDS)
    probe = _reconciler_probe(pool, build_postgres_ping, build_worker_probe, object_store_from_env)
    aux_host, aux_port = resolve_health_bind("reconciler")
    aux_app = build_aux_app(heartbeat=heartbeat, probe=probe, metric_reader=telemetry.scrape_reader)
    aux_task = asyncio.create_task(serve_aux(aux_app, host=aux_host, port=aux_port))
    provider_composition = ProviderComposition(secret_registry=secret_registry)
    provider_resolver = provider_composition.build_provider_resolver()
    discovery_task = asyncio.create_task(_register_provider_resources(pool, provider_resolver))
    console_hosting = None
    try:
        console_hosting = await provider_composition.build_reconciler_console_hosting()
        reconciler = Reconciler(
            pool,
            provider_composition.build_reconciler_reaper(),
            config=ReconcileConfig(
                upload_store=upload_store,
                image_store=upload_store,
                console_registry=console_hosting.registry if console_hosting else None,
                resetter=provider_composition.build_reconciler_transport_resetter(),
                dump_volume_reaper=provider_composition.build_reconciler_dump_volume_reaper(),
                build_vm_reaper=provider_composition.build_reconciler_build_vm_reaper(),
                build_host_prober=provider_composition.build_reconciler_build_host_prober(),
                heartbeat=heartbeat,
                telemetry=ReconcilerTelemetry(
                    tracer=telemetry.tracer_provider.get_tracer("kdive.reconciler"),
                    meter=telemetry.meter_provider.get_meter("kdive.reconciler"),
                ),
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
        await _cancel(discovery_task, aux_task)
        secret_registry.clear()
        await pool.close()


def _optional_reconciler_object_store(
    store_factory: Callable[[], ObjectStore],
) -> ObjectStore | None:
    """Return the object store, or ``None`` only when S3 is wholly unconfigured."""
    try:
        return store_factory()
    except CategorizedError:
        if _s3_env_is_absent():
            return None
        raise


def _s3_env_is_absent() -> bool:
    env = config.env_snapshot()
    return _S3_OPTIONAL_ENV_NAMES.isdisjoint(env)


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
        await asyncio.wait_for(
            resolver.register_all_discovery(pool),
            timeout=_PROVIDER_DISCOVERY_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        _log.warning(
            "reconciler: provider discovery registration timed out after %ss",
            _PROVIDER_DISCOVERY_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - registration failure must not crash the reconciler
        _log.warning("reconciler: provider discovery registration failed", exc_info=True)


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
    _COMMAND_BY_NAME[args.command].handler(args, secret_registry, telemetry)


if __name__ == "__main__":
    main()
