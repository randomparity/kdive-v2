"""Process entrypoints: `python -m kdive server|worker|reconciler` (issues #10, #12).

`server` runs the FastMCP streamable-HTTP app; `worker` runs the job-queue worker
loop; `reconciler` runs the drift-repair loop (ADR-0021). All three configure the
structured logger first (ADR-0014).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket

from kdive.db.pool import create_pool
from kdive.log import configure_logging
from kdive.version import full_version

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000

_log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with the `server`/`worker`/`reconciler` subcommands."""
    parser = argparse.ArgumentParser(prog="kdive")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("KDIVE_LOG_LEVEL", "INFO"),
        help="structured-logging level (default INFO)",
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
    return parser


async def _run_server(host: str, port: int) -> None:
    from kdive.mcp.app import build_app

    pool = create_pool()
    await pool.open()
    try:
        app = build_app(pool)
        await app.run_async(transport="http", host=host, port=port)
    finally:
        await pool.close()


async def _run_worker() -> None:
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
        worker = Worker(pool, build_handler_registry(), worker_id=worker_id)
        await worker.run(stop)
    finally:
        await pool.close()


async def _run_reconciler() -> None:
    from kdive.reconciler.loop import NullReaper, Reconciler

    pool = create_pool(min_size=1)
    await pool.open()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        reconciler = Reconciler(pool, NullReaper())
        await reconciler.run(stop)
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> None:
    """Parse arguments, configure logging, and dispatch to the chosen subcommand."""
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    _log.info("starting kdive %s (%s)", full_version(), args.command)
    if args.command == "server":
        host = os.environ.get("KDIVE_HTTP_HOST", _DEFAULT_HOST)
        port = int(os.environ.get("KDIVE_HTTP_PORT", str(_DEFAULT_PORT)))
        asyncio.run(_run_server(host, port))
    elif args.command == "worker":
        asyncio.run(_run_worker())
    elif args.command == "reconciler":
        asyncio.run(_run_reconciler())


if __name__ == "__main__":
    main()
