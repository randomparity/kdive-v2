"""Installed-package admin bootstrap helpers for local KDIVE stacks."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from kdive.admin.default_compose import LOCAL_COMPOSE
from kdive.admin.default_fixtures import LOCAL_LIBVIRT_FIXTURES
from kdive.db.migrate import apply_migrations


def local_env_defaults() -> dict[str, str]:
    """Return repo-independent defaults for a local host-run KDIVE stack."""
    home = os.environ.get("HOME", "")
    host = os.environ.get("KDIVE_HTTP_HOST", "127.0.0.1")
    port = os.environ.get("KDIVE_HTTP_PORT", "8000")
    return {
        # pragma: allowlist nextline secret
        "KDIVE_DATABASE_URL": "postgresql://kdive:kdive@localhost:5432/kdive",
        "KDIVE_OIDC_ISSUER": "http://localhost:8090/default",
        "KDIVE_OIDC_JWKS_URI": "http://localhost:8090/default/jwks",
        "KDIVE_OIDC_AUDIENCE": "kdive",
        "KDIVE_S3_ENDPOINT_URL": "http://localhost:9000",
        "KDIVE_S3_BUCKET": "kdive-artifacts",
        "KDIVE_S3_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "minioadmin",
        "AWS_SECRET_ACCESS_KEY": "minioadmin",  # pragma: allowlist secret
        "KDIVE_HTTP_HOST": host,
        "KDIVE_HTTP_PORT": port,
        "KDIVE_STACK_BASE_URL": f"http://{host}:{port}/mcp",
        "KDIVE_KERNEL_SRC": f"{home}/src/linux",
        "KDIVE_BUILD_WORKSPACE": "/var/lib/kdive/build",
        "KDIVE_BUILD_COMPONENT_ROOTS": "/var/lib/kdive/build/components:/etc/kdive/fixtures",
        "KDIVE_INSTALL_STAGING": "/var/lib/kdive/install",
        "KDIVE_FIXTURE_CATALOG_PATH": "/etc/kdive/fixtures/local-libvirt",
    }


def print_local_env() -> None:
    """Print shell exports for :func:`local_env_defaults`."""
    for key, value in local_env_defaults().items():
        print(f"export {key}={value}")


def default_fixture_files() -> Mapping[str, str]:
    """Return the embedded local-libvirt fixture files keyed by relative path."""
    return LOCAL_LIBVIRT_FIXTURES


def default_compose_text() -> str:
    """Return the embedded backing-service compose file."""
    return LOCAL_COMPOSE


def _refuse_existing(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite")


def install_fixtures(dest: Path, *, force: bool = False) -> None:
    """Install the embedded fixture catalog under ``dest``."""
    _refuse_existing(dest, force=force)
    for relative, content in LOCAL_LIBVIRT_FIXTURES.items():
        path = dest / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def install_compose(dest: Path, *, force: bool = False) -> None:
    """Install the embedded backing-service compose file at ``dest``."""
    _refuse_existing(dest, force=force)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(LOCAL_COMPOSE, encoding="utf-8")


def migrate(database_url: str | None = None) -> int:
    """Apply packaged database migrations and return the number applied."""
    url = database_url or os.environ["KDIVE_DATABASE_URL"]
    conn = psycopg.connect(url, autocommit=True)
    try:
        applied = apply_migrations(conn)
    finally:
        conn.close()
    print(f"applied {len(applied)} migration(s)")
    return len(applied)


def seed_project_statements(
    *,
    project: str,
    limit_kcu: Decimal,
    max_concurrent_allocations: int,
    max_concurrent_systems: int,
) -> list[tuple[str, Sequence[Any]]]:
    """Build parameterized budget/quota upserts for one demo project."""
    return [
        (
            "INSERT INTO budgets (project, limit_kcu, spent_kcu) "
            "VALUES (%s, %s, 0) "
            "ON CONFLICT (project) DO UPDATE SET limit_kcu = EXCLUDED.limit_kcu",
            (project, limit_kcu),
        ),
        (
            "INSERT INTO quotas (project, max_concurrent_allocations, max_concurrent_systems) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (project) DO UPDATE SET "
            "max_concurrent_allocations = EXCLUDED.max_concurrent_allocations, "
            "max_concurrent_systems = EXCLUDED.max_concurrent_systems",
            (project, max_concurrent_allocations, max_concurrent_systems),
        ),
    ]


async def seed_demo(
    *,
    project: str,
    limit_kcu: Decimal,
    max_concurrent_allocations: int,
    max_concurrent_systems: int,
) -> None:
    """Seed budget/quota rows and register the local provider resource."""
    from kdive.db.pool import create_pool

    pool = create_pool()
    await pool.open()
    try:
        async with pool.connection() as conn, conn.transaction():
            for statement, params in seed_project_statements(
                project=project,
                limit_kcu=limit_kcu,
                max_concurrent_allocations=max_concurrent_allocations,
                max_concurrent_systems=max_concurrent_systems,
            ):
                await conn.execute(statement.encode(), params)
        await register_local_resource(pool)
    finally:
        await pool.close()


async def register_local_resource(pool: Any) -> None:
    """Register the local-libvirt resource using the default provider runtime."""
    from kdive.providers.composition import build_default_provider_runtime

    await build_default_provider_runtime().register_discovery(pool)


def supervisor_commands(env: Mapping[str, str]) -> list[list[str]]:
    """Return the three host process commands for the local stack supervisor."""
    del env
    return [
        [sys.executable, "-m", "kdive", "server"],
        [sys.executable, "-m", "kdive", "worker"],
        [sys.executable, "-m", "kdive", "reconciler"],
    ]


def run_stack() -> int:
    """Run server, worker, and reconciler as child processes until one exits."""
    env = {**local_env_defaults(), **os.environ}
    children = [subprocess.Popen(cmd, env=env) for cmd in supervisor_commands(env)]  # noqa: S603

    def stop(_signum: int, _frame: object) -> None:
        for child in children:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            for child in children:
                code = child.poll()
                if code is not None:
                    for other in children:
                        if other.poll() is None:
                            other.terminate()
                    return code
            time.sleep(1)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
