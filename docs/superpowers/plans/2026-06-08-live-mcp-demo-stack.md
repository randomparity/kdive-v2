# Live MCP Demo Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a local KDIVE MCP demo stack start with sane defaults, expose usable rootfs
catalog entries, and build kernels non-interactively from a provided `.config`.

**Architecture:** Keep `server`, `worker`, and `reconciler` as host processes so libvirt and
filesystem paths still resolve on the host. Add installed-package admin entrypoints for migration,
demo seeding, and one-command demo stack startup; repo `just` recipes become developer wrappers
only. Keep rootfs as catalog/configuration, not an MCP tool.

**Tech Stack:** Python 3.13 with `uv`, FastMCP HTTP transport, Postgres migrations, MinIO,
mock OIDC, local libvirt, shell scripts checked by `shellcheck`/`shfmt`, pytest.

---

## File Map

- Create `scripts/live-stack/env.sh`: shared default environment for host KDIVE processes.
- Create `scripts/live-stack/apply-migrations.sh`: apply database migrations after compose
  backends are healthy.
- Create `scripts/live-stack/start.sh`: foreground supervisor for server, worker, and reconciler.
- Create `scripts/live-stack/stop.sh`: stop a background stack started by `start.sh --daemon`.
- Create `src/kdive/admin/bootstrap.py`: installed-package admin helpers for env printing,
  migrations, demo seeding, and stack process supervision.
- Create `src/kdive/admin/default_fixtures.py`: embedded default local-libvirt fixture catalog
  written by `python -m kdive install-fixtures`.
- Create `src/kdive/admin/default_compose.py`: embedded local backing-service compose file written
  by `python -m kdive install-compose`.
- Modify `src/kdive/__main__.py`: add `migrate`, `seed-demo`, `print-local-env`, and `stack`
  subcommands.
- Create `tests/admin/test_bootstrap.py`: admin CLI/bootstrap behavior tests.
- Create `docs/admin/local-stack.md`: user/admin documentation for bringing up KDIVE outside the
  repository without `just`.
- Create `tests/scripts/test_live_stack_scripts.py`: script hygiene and contract tests.
- Modify `justfile`: add `stack-migrate`, `stack-start`, `stack-stop`, and make `stack-up`
  print the new flow.
- Modify `docs/runbooks/live-stack.md`: replace manual three-terminal flow with the new default.
- Modify `fixtures/local-libvirt/manifest.yaml`: include default Fedora cloud and busybox entries.
- Create `fixtures/local-libvirt/rootfs/fedora-cloud-43.yaml`: catalog entry for a Fedora cloud qcow2.
- Create `fixtures/local-libvirt/rootfs/busybox-bare.yaml`: catalog entry for a bare busybox qcow2.
- Create `scripts/live-vm/build-busybox-rootfs.sh`: build the bare busybox qcow2 image.
- Create `scripts/live-vm/fetch-fedora-cloud-image.sh`: fetch/cache a Fedora cloud qcow2.
- Create `tests/components/test_default_fixture_catalog.py`: verify the shipped catalog entries load.
- Modify `src/kdive/components/catalog.py`: support `KDIVE_FIXTURE_CATALOG_PATH` so operators can
  provide their own rootfs catalog without patching the repo.
- Modify `scripts/live-vm/build-guest-image.sh`: align its default output with the shipped catalog.
- Modify `src/kdive/providers/local_libvirt/provisioning.py`: load fixture catalog storage paths and
  ensure console log files are readable by the KDIVE user before libvirt starts.
- Modify `src/kdive/providers/local_libvirt/install.py`: classify disappeared KDIVE domains as
  terminal during readiness polling.
- Modify `src/kdive/providers/local_libvirt/build.py`: run `make olddefconfig` after staging `.config`
  and before config validation/build.
- Modify `tests/providers/local_libvirt/test_build.py`: cover olddefconfig ordering and failures.
- Modify `tests/providers/local_libvirt/test_install.py`: cover disappeared-domain readiness behavior.
- Modify `tests/providers/local_libvirt/test_provisioning.py`: cover console log pre-creation.

## Task 0: Installed Admin Commands And Deployment Docs

**Files:**
- Create: `src/kdive/admin/bootstrap.py`
- Create: `src/kdive/admin/default_fixtures.py`
- Create: `src/kdive/admin/default_compose.py`
- Modify: `src/kdive/__main__.py`
- Create: `tests/admin/test_bootstrap.py`
- Create: `docs/admin/local-stack.md`

- [ ] **Step 1: Write admin bootstrap tests**

Create `tests/admin/test_bootstrap.py`:

```python
import asyncio
import os
from decimal import Decimal
from pathlib import Path

import pytest

from kdive.admin.bootstrap import (
    default_compose_text,
    default_fixture_files,
    install_compose,
    install_fixtures,
    local_env_defaults,
    seed_demo,
    seed_project_statements,
    supervisor_commands,
)


def test_local_env_defaults_are_repo_independent(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/home/operator")

    env = local_env_defaults()

    assert env["KDIVE_DATABASE_URL"] == "postgresql://kdive:kdive@localhost:5432/kdive"  # pragma: allowlist secret
    assert env["KDIVE_STACK_BASE_URL"] == "http://127.0.0.1:8000/mcp"
    assert env["KDIVE_KERNEL_SRC"] == "/home/operator/src/linux"
    assert "/home/operator/src/kdive" not in " ".join(env.values())


def test_seed_project_sql_contains_budget_and_quota_upserts() -> None:
    statements = seed_project_statements(
        project="demo",
        limit_kcu=Decimal("1000000"),
        max_concurrent_allocations=4,
        max_concurrent_systems=4,
    )

    joined = "\n".join(statement for statement, _params in statements)
    assert "INSERT INTO budgets" in joined
    assert "INSERT INTO quotas" in joined
    assert "ON CONFLICT" in joined


def test_seed_project_sql_params_are_parameterized() -> None:
    statements = seed_project_statements(
        project="demo'; drop table budgets; --",
        limit_kcu=Decimal("1000000"),
        max_concurrent_allocations=4,
        max_concurrent_systems=4,
    )

    joined = "\n".join(statement for statement, _params in statements)

    assert "drop table" not in joined.lower()
    assert any("demo'; drop table budgets; --" in params for _statement, params in statements)


def test_seed_demo_registers_local_resource(monkeypatch, migrated_url: str) -> None:
    calls: list[str] = []

    async def fake_register(pool) -> None:
        del pool
        calls.append("registered")

    monkeypatch.setenv("KDIVE_DATABASE_URL", migrated_url)
    monkeypatch.setattr(
        "kdive.admin.bootstrap.register_local_resource",
        fake_register,
    )

    asyncio.run(
        seed_demo(
            project="demo",
            limit_kcu=Decimal("1000000"),
            max_concurrent_allocations=4,
            max_concurrent_systems=4,
        )
    )

    assert calls == ["registered"]


def test_supervisor_commands_start_all_processes() -> None:
    commands = supervisor_commands(os.environ.copy())

    assert [cmd[-1] for cmd in commands] == ["server", "worker", "reconciler"]
    assert all(cmd[:3] == [os.sys.executable, "-m", "kdive"] for cmd in commands)


def test_default_fixture_files_include_catalog() -> None:
    fixture_files = default_fixture_files()

    assert "manifest.yaml" in fixture_files
    assert "profiles/console-ready_x86_64.yaml" in fixture_files


def test_default_compose_includes_required_backends() -> None:
    compose = default_compose_text()

    assert "postgres:" in compose
    assert "minio:" in compose
    assert "oidc:" in compose
    assert "minio-init:" in compose


def test_install_helpers_refuse_overwrite_without_force(tmp_path: Path) -> None:
    fixture_dest = tmp_path / "fixtures"
    compose_dest = tmp_path / "compose.yml"
    (fixture_dest / "manifest.yaml").parent.mkdir(parents=True)
    (fixture_dest / "manifest.yaml").write_text("custom", encoding="utf-8")
    compose_dest.write_text("custom", encoding="utf-8")

    with pytest.raises(FileExistsError):
        install_fixtures(fixture_dest)
    with pytest.raises(FileExistsError):
        install_compose(compose_dest)

    install_fixtures(fixture_dest, force=True)
    install_compose(compose_dest, force=True)
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
uv run python -m pytest tests/admin/test_bootstrap.py -q
```

Expected: fail because `kdive.admin.bootstrap` does not exist.

- [ ] **Step 3: Add admin bootstrap module**

Create `src/kdive/admin/bootstrap.py`:

```python
"""Installed-package admin bootstrap helpers for local KDIVE stacks."""

from __future__ import annotations

import asyncio
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

from kdive.db.migrate import apply_migrations
from kdive.admin.default_compose import LOCAL_COMPOSE
from kdive.admin.default_fixtures import LOCAL_LIBVIRT_FIXTURES


def local_env_defaults() -> dict[str, str]:
    home = os.environ.get("HOME", "")
    host = os.environ.get("KDIVE_HTTP_HOST", "127.0.0.1")
    port = os.environ.get("KDIVE_HTTP_PORT", "8000")
    return {
        "KDIVE_DATABASE_URL": "postgresql://kdive:kdive@localhost:5432/kdive",  # pragma: allowlist secret
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
    for key, value in local_env_defaults().items():
        print(f"export {key}={value}")


def default_fixture_files() -> Mapping[str, str]:
    return LOCAL_LIBVIRT_FIXTURES


def default_compose_text() -> str:
    return LOCAL_COMPOSE


def _refuse_existing(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite")


def install_fixtures(dest: Path, *, force: bool = False) -> None:
    _refuse_existing(dest, force=force)
    for relative, content in LOCAL_LIBVIRT_FIXTURES.items():
        path = dest / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def install_compose(dest: Path, *, force: bool = False) -> None:
    _refuse_existing(dest, force=force)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(LOCAL_COMPOSE, encoding="utf-8")


def migrate(database_url: str | None = None) -> int:
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
                await conn.execute(statement, params)
        await register_local_resource(pool)
    finally:
        await pool.close()


async def register_local_resource(pool) -> None:
    from kdive.providers.composition import build_default_provider_runtime

    await build_default_provider_runtime().register_discovery(pool)


def supervisor_commands(env: Mapping[str, str]) -> list[list[str]]:
    return [
        [sys.executable, "-m", "kdive", "server"],
        [sys.executable, "-m", "kdive", "worker"],
        [sys.executable, "-m", "kdive", "reconciler"],
    ]


def run_stack() -> int:
    env = {**local_env_defaults(), **os.environ}
    children = [subprocess.Popen(cmd, env=env) for cmd in supervisor_commands(env)]

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
```

- [ ] **Step 4: Add embedded default fixture catalog**

Create `src/kdive/admin/default_fixtures.py`:

```python
"""Default fixture catalog files installed by `python -m kdive install-fixtures`."""

from __future__ import annotations

LOCAL_LIBVIRT_FIXTURES: dict[str, str] = {
    "manifest.yaml": """schema_version: 1
provider: local-libvirt
storage:
  allowed_component_roots:
    - /var/lib/kdive/rootfs
  cache_dir: /var/lib/kdive/rootfs/cache
  overlay_dir: /var/lib/kdive/rootfs/overlays
rootfs:
  - rootfs/fedora-kdive-ready-43.yaml
profiles:
  - profiles/console-ready_x86_64.yaml
""",
    "rootfs/fedora-kdive-ready-43.yaml": """provider: local-libvirt
name: fedora-kdive-ready-43
arch: x86_64
format: qcow2
root_device: /dev/vda
source:
  kind: local
  path: /var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2
visibility: public
capabilities:
  - kdive-ready-console
  - ssh
  - drgn
""",
    "profiles/console-ready_x86_64.yaml": """provider: local-libvirt
name: console-ready_x86_64
arch: x86_64
requires:
  config:
    required:
      CONFIG_SERIAL_8250_CONSOLE: y
      CONFIG_VIRTIO_BLK: y
      CONFIG_VIRTIO_PCI: y
  cmdline:
    required_tokens:
      - console=ttyS0
      - root=/dev/vda
    protected_prefixes:
      - console=
      - root=
      - crashkernel=
  rootfs:
    format: qcow2
    root_device: /dev/vda
    capabilities:
      - kdive-ready-console
""",
}
```

- [ ] **Step 5: Add embedded local compose file**

Create `src/kdive/admin/default_compose.py`:

```python
"""Default local backing-service compose file installed by `python -m kdive install-compose`."""

from __future__ import annotations

LOCAL_COMPOSE = """services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: kdive
      POSTGRES_PASSWORD: kdive # pragma: allowlist secret
      POSTGRES_DB: kdive
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U kdive"]
      interval: 5s
      timeout: 5s
      retries: 20

  minio:
    image: minio/minio:RELEASE.2025-04-22T22-12-26Z
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin # pragma: allowlist secret
    ports:
      - "9000:9000"
      - "9001:9001"
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 5s
      timeout: 5s
      retries: 20

  minio-init:
    image: minio/mc:RELEASE.2025-04-16T18-13-26Z
    depends_on:
      minio:
        condition: service_healthy
    entrypoint:
      - /bin/sh
      - -c
      - |
        mc alias set local http://minio:9000 minioadmin minioadmin # pragma: allowlist secret
        mc mb --ignore-existing local/kdive-artifacts

  oidc:
    image: ghcr.io/navikt/mock-oauth2-server:3.0.3
    ports:
      - "8090:8080"
    environment:
      SERVER_PORT: "8080"
"""
```

The embedded compose content should mirror `docker-compose.yml`; update both together when image
tags change so source-tree and installed-package demos stay equivalent.

- [ ] **Step 6: Wire CLI subcommands**

Modify `src/kdive/__main__.py`:

```python
    sub.add_parser("migrate", help="apply database migrations")
    fixtures = sub.add_parser("install-fixtures", help="install default fixture catalog")
    fixtures.add_argument("--dest", default="/etc/kdive/fixtures/local-libvirt")
    fixtures.add_argument("--force", action="store_true", help="overwrite existing files")
    compose = sub.add_parser("install-compose", help="install local backing-service compose file")
    compose.add_argument("--dest", default="/etc/kdive/docker-compose.local.yml")
    compose.add_argument("--force", action="store_true", help="overwrite an existing file")

    seed = sub.add_parser("seed-demo", help="seed a project for local agent demos")
    seed.add_argument("--project", default="demo")
    seed.add_argument("--limit-kcu", default="1000000")
    seed.add_argument("--max-concurrent-allocations", type=int, default=4)
    seed.add_argument("--max-concurrent-systems", type=int, default=4)

    sub.add_parser("print-local-env", help="print local demo KDIVE_* defaults")
    sub.add_parser("stack", help="run server, worker, and reconciler under one supervisor")
```

Add dispatch:

```python
    elif args.command == "migrate":
        from kdive.admin.bootstrap import migrate

        migrate()
    elif args.command == "install-fixtures":
        from pathlib import Path

        from kdive.admin.bootstrap import install_fixtures

        install_fixtures(Path(args.dest), force=args.force)
    elif args.command == "install-compose":
        from pathlib import Path

        from kdive.admin.bootstrap import install_compose

        install_compose(Path(args.dest), force=args.force)
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
    elif args.command == "print-local-env":
        from kdive.admin.bootstrap import print_local_env

        print_local_env()
    elif args.command == "stack":
        from kdive.admin.bootstrap import run_stack

        raise SystemExit(run_stack())
```

- [ ] **Step 7: Add outside-repo admin docs**

Create `docs/admin/local-stack.md` with this deployment flow:

````markdown
# Local KDIVE Stack Administration

This guide assumes KDIVE is installed as a Python package on the libvirt host. It does not
use `just` or require running from a source checkout.

## Backing Services

Install the local backing-service compose file:

```bash
python -m kdive install-compose --dest /etc/kdive/docker-compose.local.yml
```

Start the backing services:

```bash
docker compose -f /etc/kdive/docker-compose.local.yml up -d --wait postgres minio oidc
docker compose -f /etc/kdive/docker-compose.local.yml run --rm minio-init
```

Production-like deployments may replace these containers with managed Postgres, managed
S3-compatible object storage, and a real OIDC issuer. The KDIVE processes only require the
environment variables below.

## Environment

Install the default local-libvirt fixture catalog:

```bash
python -m kdive install-fixtures --dest /etc/kdive/fixtures/local-libvirt
```

Print local defaults:

```bash
python -m kdive print-local-env > /etc/kdive/local.env
```

Review `/etc/kdive/local.env`, especially:

- `KDIVE_DATABASE_URL`
- `KDIVE_OIDC_*`
- `KDIVE_S3_*`
- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
- `KDIVE_KERNEL_SRC`
- `KDIVE_FIXTURE_CATALOG_PATH`

## Schema

```bash
set -a
. /etc/kdive/local.env
set +a
python -m kdive migrate
```

## Seed A Demo Project

```bash
python -m kdive seed-demo \
  --project demo \
  --limit-kcu 1000000 \
  --max-concurrent-allocations 4 \
  --max-concurrent-systems 4
```

This creates the budget/quota rows needed for agent allocations and registers the local
libvirt resource discovered on the host.

## Start The Stack

Demo supervisor:

```bash
python -m kdive stack
```

Production-style process split:

```bash
python -m kdive server
python -m kdive worker
python -m kdive reconciler
```

Use a process manager such as systemd for the split mode.
````

- [ ] **Step 8: Verify admin commands**

Run:

```bash
uv run python -m pytest tests/admin/test_bootstrap.py -q
uv run python -m kdive --help
uv run python -m kdive install-fixtures --dest /tmp/kdive-fixtures
uv run python -m kdive install-compose --dest /tmp/kdive-compose.yml
uv run python -m kdive print-local-env | rg "KDIVE_STACK_BASE_URL"
```

Expected: tests pass, help lists the new subcommands, and env printing works from the package.

## Task 1: Stack Defaults And Migration Bootstrap

**Files:**
- Create: `scripts/live-stack/env.sh`
- Create: `scripts/live-stack/apply-migrations.sh`
- Modify: `justfile`
- Test: `tests/scripts/test_live_stack_scripts.py`

- [ ] **Step 1: Write script contract tests**

Add these tests in `tests/scripts/test_live_stack_scripts.py`:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_live_stack_env_exports_required_defaults() -> None:
    env = (ROOT / "scripts/live-stack/env.sh").read_text()
    required = [
        "KDIVE_DATABASE_URL",
        "KDIVE_OIDC_ISSUER",
        "KDIVE_OIDC_JWKS_URI",
        "KDIVE_OIDC_AUDIENCE",
        "KDIVE_S3_ENDPOINT_URL",
        "KDIVE_S3_BUCKET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "KDIVE_BUILD_WORKSPACE",
        "KDIVE_BUILD_COMPONENT_ROOTS",
        "KDIVE_INSTALL_STAGING",
        "KDIVE_STACK_BASE_URL",
    ]
    for name in required:
        assert f"export {name}=" in env


def test_live_stack_scripts_are_strict_bash() -> None:
    for name in ("env.sh", "apply-migrations.sh", "start.sh", "stop.sh"):
        text = (ROOT / "scripts/live-stack" / name).read_text()
        assert text.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
uv run python -m pytest tests/scripts/test_live_stack_scripts.py -q
```

Expected: fail because `scripts/live-stack/*.sh` do not exist.

- [ ] **Step 3: Add shared environment script**

Create `scripts/live-stack/env.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
default_database_url="postgresql://kdive:kdive@localhost:5432/kdive" # pragma: allowlist secret

export KDIVE_DATABASE_URL="${KDIVE_DATABASE_URL:-${default_database_url}}"
export KDIVE_OIDC_ISSUER="${KDIVE_OIDC_ISSUER:-http://localhost:8090/default}"
export KDIVE_OIDC_JWKS_URI="${KDIVE_OIDC_JWKS_URI:-http://localhost:8090/default/jwks}"
export KDIVE_OIDC_AUDIENCE="${KDIVE_OIDC_AUDIENCE:-kdive}"
export KDIVE_S3_ENDPOINT_URL="${KDIVE_S3_ENDPOINT_URL:-http://localhost:9000}"
export KDIVE_S3_BUCKET="${KDIVE_S3_BUCKET:-kdive-artifacts}"
export KDIVE_S3_REGION="${KDIVE_S3_REGION:-us-east-1}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-minioadmin}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-minioadmin}"
export KDIVE_HTTP_HOST="${KDIVE_HTTP_HOST:-127.0.0.1}"
export KDIVE_HTTP_PORT="${KDIVE_HTTP_PORT:-8000}"
export KDIVE_STACK_BASE_URL="${KDIVE_STACK_BASE_URL:-http://${KDIVE_HTTP_HOST}:${KDIVE_HTTP_PORT}/mcp}"
export KDIVE_BUILD_WORKSPACE="${KDIVE_BUILD_WORKSPACE:-${repo_root}/.live-build}"
export KDIVE_BUILD_COMPONENT_ROOTS="${KDIVE_BUILD_COMPONENT_ROOTS:-${repo_root}/fixtures/local-libvirt:${repo_root}/.live-components}"
export KDIVE_INSTALL_STAGING="${KDIVE_INSTALL_STAGING:-/var/lib/kdive/install}"
export KDIVE_KERNEL_SRC="${KDIVE_KERNEL_SRC:-${HOME}/src/linux}"
```

- [ ] **Step 4: Add migration helper**

Create `scripts/live-stack/apply-migrations.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${repo_root}/scripts/live-stack/env.sh"

uv run python - <<'PY'
import psycopg

from kdive.db.migrate import apply_migrations

conn = psycopg.connect(__import__("os").environ["KDIVE_DATABASE_URL"], autocommit=True)
try:
    applied = apply_migrations(conn)
finally:
    conn.close()

print(f"applied {len(applied)} migration(s)")
PY
```

- [ ] **Step 5: Wire `just` recipes**

Modify `justfile`:

```just
stack-migrate:
    ./scripts/live-stack/apply-migrations.sh

stack-up:
    docker compose up -d --wait postgres minio oidc
    docker compose run --rm minio-init
    ./scripts/live-stack/apply-migrations.sh
    @echo "Backends healthy and schema migrated."
    @echo "Start host processes with: python -m kdive stack"
    @echo "MCP URL: http://127.0.0.1:8000/mcp"
    @echo "Full runbook: docs/runbooks/live-stack.md"
```

- [ ] **Step 6: Run focused verification**

Run:

```bash
uv run python -m pytest tests/scripts/test_live_stack_scripts.py -q
just lint-shell
```

Expected: pytest passes; shell lint passes for the new scripts.

## Task 2: Supervised Host Process Startup

**Files:**
- Create: `scripts/live-stack/start.sh`
- Create: `scripts/live-stack/stop.sh`
- Modify: `justfile`
- Modify: `docs/runbooks/live-stack.md`
- Test: `tests/scripts/test_live_stack_scripts.py`

- [ ] **Step 1: Extend tests for supervisor contract**

Append:

```python
def test_stack_start_runs_all_three_kdive_processes() -> None:
    text = (ROOT / "scripts/live-stack/start.sh").read_text()
    assert "python -m kdive server" in text
    assert "python -m kdive worker" in text
    assert "python -m kdive reconciler" in text
    assert "trap cleanup EXIT INT TERM" in text


def test_stack_stop_uses_pid_file_not_process_name_patterns() -> None:
    text = (ROOT / "scripts/live-stack/stop.sh").read_text()
    assert "KDIVE_STACK_PID_FILE" in text
    assert "pkill" not in text
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
uv run python -m pytest tests/scripts/test_live_stack_scripts.py -q
```

Expected: fail because `start.sh` and `stop.sh` do not exist yet.

- [ ] **Step 3: Add foreground/daemon stack starter**

Create `scripts/live-stack/start.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${repo_root}/scripts/live-stack/env.sh"

pid_file="${KDIVE_STACK_PID_FILE:-${repo_root}/.live-stack.pid}"
log_dir="${KDIVE_STACK_LOG_DIR:-${repo_root}/.live-stack-logs}"
mode="${1:-foreground}"

mkdir -p "${log_dir}"

cleanup() {
  if [[ -f "${pid_file}" ]]; then
    while read -r pid; do
      [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null || true
    done <"${pid_file}"
    rm -f "${pid_file}"
  fi
}
trap cleanup EXIT INT TERM

start_one() {
  local name="$1"
  shift
  "$@" >"${log_dir}/${name}.log" 2>&1 &
  echo "$!" >>"${pid_file}"
}

rm -f "${pid_file}"
start_one server uv run python -m kdive server
start_one worker uv run python -m kdive worker
start_one reconciler uv run python -m kdive reconciler

echo "KDIVE MCP stack started"
echo "MCP URL: ${KDIVE_STACK_BASE_URL}"
echo "Logs: ${log_dir}"

if [[ "${mode}" == "--daemon" ]]; then
  trap - EXIT INT TERM
  exit 0
fi

wait
```

- [ ] **Step 4: Add stop helper**

Create `scripts/live-stack/stop.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
pid_file="${KDIVE_STACK_PID_FILE:-${repo_root}/.live-stack.pid}"

if [[ ! -f "${pid_file}" ]]; then
  echo "no KDIVE stack pid file at ${pid_file}"
  exit 0
fi

while read -r pid; do
  [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null || true
done <"${pid_file}"
rm -f "${pid_file}"
```

- [ ] **Step 5: Wire `just` recipes**

Add:

```just
stack-start:
    ./scripts/live-stack/start.sh

stack-start-daemon:
    ./scripts/live-stack/start.sh --daemon

stack-stop:
    ./scripts/live-stack/stop.sh
```

- [ ] **Step 6: Update runbook**

Change `docs/runbooks/live-stack.md` so contributor startup is explicitly a wrapper around
the installed-package commands:

```bash
python -m kdive migrate
python -m kdive seed-demo --project demo
python -m kdive stack
```

Keep a short note that `just stack-up` / `just stack-start` are source-tree conveniences,
not deployment requirements. Point admins to `docs/admin/local-stack.md`.

- [ ] **Step 7: Verify**

Run:

```bash
uv run python -m pytest tests/scripts/test_live_stack_scripts.py -q
just lint-shell
```

Expected: tests and shell lint pass.

## Task 3: Rootfs Catalog Defaults

**Files:**
- Modify: `fixtures/local-libvirt/manifest.yaml`
- Create: `fixtures/local-libvirt/rootfs/fedora-cloud-43.yaml`
- Create: `fixtures/local-libvirt/rootfs/busybox-bare.yaml`
- Modify: `src/kdive/admin/default_fixtures.py`
- Create: `scripts/live-vm/fetch-fedora-cloud-image.sh`
- Modify: `scripts/live-vm/build-guest-image.sh`
- Create: `scripts/live-vm/build-busybox-rootfs.sh`
- Modify: `src/kdive/components/catalog.py`
- Test: `tests/components/test_default_fixture_catalog.py`
- Test: `tests/scripts/test_live_vm_fixtures.py`

- [ ] **Step 1: Add catalog tests**

Create `tests/components/test_default_fixture_catalog.py`:

```python
from pathlib import Path

import pytest

from kdive.components.catalog import DEFAULT_FIXTURE_CATALOG_PATH, load_fixture_catalog


def test_default_catalog_exposes_expected_rootfs_entries() -> None:
    catalog = load_fixture_catalog(DEFAULT_FIXTURE_CATALOG_PATH)
    names = {entry.name for entry in catalog.rootfs_for_provider("local-libvirt")}
    assert "fedora-kdive-ready-43" in names
    assert "fedora-cloud-43" in names
    assert "busybox-bare" in names


def test_default_catalog_rootfs_entries_are_qcow2_vda() -> None:
    catalog = load_fixture_catalog(DEFAULT_FIXTURE_CATALOG_PATH)
    for name in ("fedora-kdive-ready-43", "fedora-cloud-43", "busybox-bare"):
        entry = catalog.rootfs_entry("local-libvirt", name)
        assert entry is not None
        assert entry.format == "qcow2"
        assert entry.root_device == "/dev/vda"
        assert entry.source.kind == "local"


def test_catalog_path_can_be_overridden_by_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = tmp_path / "catalog"
    (fixture / "rootfs").mkdir(parents=True)
    (fixture / "manifest.yaml").write_text(
        "schema_version: 1\n"
        "provider: local-libvirt\n"
        "storage:\n"
        "  allowed_component_roots: [/tmp/rootfs]\n"
        "  cache_dir: /tmp/rootfs/cache\n"
        "  overlay_dir: /tmp/rootfs/overlays\n"
        "rootfs: [rootfs/custom.yaml]\n"
        "profiles: []\n"
    )
    (fixture / "rootfs" / "custom.yaml").write_text(
        "provider: local-libvirt\n"
        "name: custom-rootfs\n"
        "arch: x86_64\n"
        "format: qcow2\n"
        "root_device: /dev/vda\n"
        "source:\n"
        "  kind: local\n"
        "  path: /tmp/rootfs/custom.qcow2\n"
        "visibility: public\n"
    )
    monkeypatch.setenv("KDIVE_FIXTURE_CATALOG_PATH", str(fixture))

    catalog = load_fixture_catalog()

    assert catalog.rootfs_entry("local-libvirt", "custom-rootfs") is not None
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
uv run python -m pytest tests/components/test_default_fixture_catalog.py -q
```

Expected: fail until the new entries are added.

- [ ] **Step 3: Add catalog entries**

Update `fixtures/local-libvirt/manifest.yaml`:

```yaml
rootfs:
  - rootfs/fedora-kdive-ready-43.yaml
  - rootfs/fedora-cloud-43.yaml
  - rootfs/busybox-bare.yaml
```

Create `fixtures/local-libvirt/rootfs/fedora-cloud-43.yaml`:

```yaml
provider: local-libvirt
name: fedora-cloud-43
arch: x86_64
format: qcow2
root_device: /dev/vda
source:
  kind: local
  path: /var/lib/kdive/rootfs/local/fedora-cloud-43.qcow2
visibility: public
capabilities:
  - cloud-init
  - ssh
```

Create `fixtures/local-libvirt/rootfs/busybox-bare.yaml`:

```yaml
provider: local-libvirt
name: busybox-bare
arch: x86_64
format: qcow2
root_device: /dev/vda
source:
  kind: local
  path: /var/lib/kdive/rootfs/local/busybox-bare.qcow2
visibility: public
capabilities:
  - console
  - busybox
```

- [ ] **Step 4: Mirror new catalog entries in embedded install fixtures**

Update `src/kdive/admin/default_fixtures.py` so `LOCAL_LIBVIRT_FIXTURES["manifest.yaml"]`
contains:

```yaml
rootfs:
  - rootfs/fedora-kdive-ready-43.yaml
  - rootfs/fedora-cloud-43.yaml
  - rootfs/busybox-bare.yaml
```

Add matching `LOCAL_LIBVIRT_FIXTURES` keys for:

```text
rootfs/fedora-cloud-43.yaml
rootfs/busybox-bare.yaml
```

The embedded YAML content must match the source-tree files in Step 3.

- [ ] **Step 5: Support operator-provided catalog path**

Modify `src/kdive/components/catalog.py`:

```python
import os
```

Add:

```python
_FIXTURE_CATALOG_ENV = "KDIVE_FIXTURE_CATALOG_PATH"


def fixture_catalog_path_from_env() -> Path:
    raw = os.environ.get(_FIXTURE_CATALOG_ENV)
    if raw is None or raw == "":
        return DEFAULT_FIXTURE_CATALOG_PATH
    return Path(raw)
```

Change the loader signature and first line:

```python
def load_fixture_catalog(path: Path | None = None) -> FixtureCatalog:
    """Read and validate one provider fixture catalog bundle."""
    path = path or fixture_catalog_path_from_env()
```

This makes the existing provisioning/build profile lookups honor an operator-provided
catalog path without changing MCP tool schemas.

- [ ] **Step 6: Align Fedora kdive-ready output path**

Change `scripts/live-vm/build-guest-image.sh` default:

```bash
ROOTFS_PATH="${KDIVE_ROOTFS:-/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2}"
```

- [ ] **Step 7: Add Fedora cloud image fetcher**

Create `scripts/live-vm/fetch-fedora-cloud-image.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

RELEASE="${KDIVE_FEDORA_CLOUD_RELEASE:-43}"
ARCH="${KDIVE_FEDORA_CLOUD_ARCH:-x86_64}"
DEST="${KDIVE_FEDORA_CLOUD_IMAGE:-/var/lib/kdive/rootfs/local/fedora-cloud-${RELEASE}.qcow2}"
URL="${KDIVE_FEDORA_CLOUD_IMAGE_URL:-}"
SHA256="${KDIVE_FEDORA_CLOUD_IMAGE_SHA256:-}"

if [[ -e "${DEST}" ]]; then
  echo "fedora cloud image already present at ${DEST}; leaving as-is." >&2
  exit 0
fi

if [[ -z "${URL}" ]]; then
  echo "error: KDIVE_FEDORA_CLOUD_IMAGE_URL is required for the first fetch." >&2
  echo "       Set it to the Fedora Cloud qcow2 image URL for release ${RELEASE}/${ARCH}." >&2
  exit 1
fi

for tool in curl qemu-img; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "error: ${tool} is required to fetch ${DEST}" >&2
    exit 1
  fi
done

parent="$(realpath -m -- "$(dirname -- "${DEST}")")"
mkdir -p "${parent}"
tmp="${DEST}.part"
trap 'rm -f "${tmp}"' EXIT

curl --fail --location --output "${tmp}" "${URL}"
if [[ -n "${SHA256}" ]]; then
  actual="$(sha256sum "${tmp}" | awk '{print $1}')"
  if [[ "${actual}" != "${SHA256}" ]]; then
    echo "error: checksum mismatch for ${URL}" >&2
    echo "       expected ${SHA256}" >&2
    echo "       actual   ${actual}" >&2
    exit 1
  fi
fi

qemu-img info --output=json "${tmp}" >/dev/null
chmod 0644 "${tmp}"
mv "${tmp}" "${DEST}"
echo "fedora cloud image ready at ${DEST}" >&2
```

The URL is explicit on first fetch because Fedora mirror paths and current cloud releases change;
the catalog records the local image path that KDIVE consumes.

- [ ] **Step 8: Add busybox rootfs builder**

Create `scripts/live-vm/build-busybox-rootfs.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOTFS_PATH="${KDIVE_BUSYBOX_ROOTFS:-/var/lib/kdive/rootfs/local/busybox-bare.qcow2}"
IMAGE_SIZE="${KDIVE_BUSYBOX_ROOTFS_SIZE:-256M}"

if [[ -e "${ROOTFS_PATH}" ]]; then
  echo "busybox rootfs image already present at ${ROOTFS_PATH}; leaving as-is." >&2
  exit 0
fi

for tool in busybox virt-make-fs; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "error: ${tool} is required to build ${ROOTFS_PATH}" >&2
    exit 1
  fi
done

rootfs_parent="$(realpath -m -- "$(dirname -- "${ROOTFS_PATH}")")"
mkdir -p "${rootfs_parent}"
if [[ ! -w "${rootfs_parent}" ]]; then
  echo "error: output directory '${rootfs_parent}' is not writable by the current user." >&2
  exit 1
fi

scratch="$(mktemp -d)"
cleanup() { rm -rf "${scratch}"; }
trap cleanup EXIT

mkdir -p "${scratch}"/{bin,dev,etc,proc,sys}
busybox --install -s "${scratch}/bin"
cat >"${scratch}/etc/inittab" <<'EOF'
::sysinit:/bin/mount -t proc proc /proc
::sysinit:/bin/mount -t sysfs sysfs /sys
::respawn:/bin/sh
EOF

virt-make-fs --type=ext4 --format=qcow2 --size="${IMAGE_SIZE}" "${scratch}" "${ROOTFS_PATH}"
chmod 0644 "${ROOTFS_PATH}"
echo "busybox rootfs image ready at ${ROOTFS_PATH}" >&2
```

- [ ] **Step 9: Verify catalog and script hygiene**

Run:

```bash
uv run python -m pytest tests/components/test_default_fixture_catalog.py tests/components/test_catalog.py -q
just lint-shell
```

Expected: tests and shell lint pass.

## Task 4: Provider Path Defaults And Console Observability

**Files:**
- Modify: `src/kdive/providers/local_libvirt/provisioning.py`
- Modify: `src/kdive/providers/local_libvirt/install.py`
- Test: `tests/providers/local_libvirt/test_provisioning.py`
- Test: `tests/providers/local_libvirt/test_install.py`

- [ ] **Step 1: Add provisioning test for console pre-creation**

Add to `tests/providers/local_libvirt/test_provisioning.py`:

```python
def test_provision_prepares_console_log_before_define() -> None:
    calls: list[tuple[str, str]] = []

    def prepare(path: Path) -> None:
        calls.append(("prepare", path.name))

    class RecordingConn(_ProvConn):
        def defineXML(self, xml: str) -> _ProvDomain:
            calls.append(("define", "xml"))
            return super().defineXML(xml)

    conn = RecordingConn()
    LocalLibvirtProvisioning(
        connect=lambda: conn,
        make_overlay=lambda _base, _overlay: None,
        overlay_exists=lambda _overlay: False,
        materialize_rootfs=lambda _rootfs, _system_id: "/var/lib/kdive/rootfs/base.qcow2",
        prepare_console_log=prepare,
    ).provision(_SYS, _profile())

    assert calls == [("prepare", f"{_SYS}.log"), ("define", "xml")]
```

- [ ] **Step 2: Add install readiness test for missing domain**

Add to `tests/providers/local_libvirt/test_install.py`:

```python
def test_real_readiness_treats_missing_domain_as_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "read_console_log", lambda path: b"")
    monkeypatch.setattr(install, "_domain_exited", lambda name: True)

    result = install._real_readiness(UUID("22222222-2222-2222-2222-222222222222"))

    assert result.answered is True
    assert result.ok is False
```

- [ ] **Step 3: Implement console pre-creation**

Add the seam type and helper in `provisioning.py`:

```python
type PrepareConsoleLog = Callable[[Path], None]


def _prepare_console_log(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o644, exist_ok=True)
        path.chmod(0o644)
    except OSError as exc:
        raise CategorizedError(
            "failed to prepare libvirt console log",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"path": str(path)},
        ) from exc
```

Add `prepare_console_log: PrepareConsoleLog = _prepare_console_log` to
`LocalLibvirtProvisioning.__init__`, store it as `self._prepare_console_log`, and call
`self._prepare_console_log(console_log_path(system_id))` after overlay creation and before
`defineXML`. Keep the path from `console_log_path(system_id)` so the domain XML and reader use
the same file.

- [ ] **Step 4: Treat missing KDIVE domains as terminal**

Change `_domain_exited()` in `install.py` so a non-zero `virsh domstate` with stderr that
contains `failed to get domain` for the KDIVE domain returns `True`. Keep timeout/probe
launch failures as `False`.

- [ ] **Step 5: Verify**

Run:

```bash
uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py tests/providers/local_libvirt/test_install.py -q
```

Expected: focused provider tests pass.

## Task 5: Non-Interactive `olddefconfig` Build Flow

**Files:**
- Modify: `src/kdive/providers/local_libvirt/build.py`
- Test: `tests/providers/local_libvirt/test_build.py`

- [ ] **Step 1: Add build ordering test**

In `tests/providers/local_libvirt/test_build.py`, first extend `_Seams`:

```python
    olddefconfig_returncode: int = 0
    olddefconfig_calls: int = 0
    call_order: list[str] = field(default_factory=list)
```

Update its methods:

```python
    def checkout(self, run_id: UUID, profile: ServerBuildProfile, workspace: Path) -> None:
        self.checkout_calls += 1
        self.call_order.append("checkout")

    def run_olddefconfig(self, workspace: Path) -> int:
        self.olddefconfig_calls += 1
        self.call_order.append("olddefconfig")
        return self.olddefconfig_returncode

    def read_config(self, workspace: Path) -> str:
        self.call_order.append("read_config")
        return self.config_text

    def run_make(self, workspace: Path) -> int:
        self.make_calls += 1
        self.call_order.append("make")
        return self.make_returncode
```

Update `_builder(...)` to pass `run_olddefconfig=seams.run_olddefconfig`.

Then add:

```python
def test_build_runs_olddefconfig_before_config_validation(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams()

    _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert seams.call_order[:3] == ["checkout", "olddefconfig", "read_config"]
    assert seams.call_order[-1] == "make"
```

- [ ] **Step 2: Add olddefconfig failure test**

```python
def test_build_maps_olddefconfig_failure_to_build_failure(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams(olddefconfig_returncode=2)

    with pytest.raises(CategorizedError) as exc:
        _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert seams.make_calls == 0
    assert store.puts == []
```

- [ ] **Step 3: Update provider seam and implementation**

In `build.py`, add:

```python
type _RunOlddefconfig = Callable[[Path], int]
```

Add `run_olddefconfig` to `LocalLibvirtBuild.__init__`, store it, and set
`run_olddefconfig=_real_run_olddefconfig` in `from_env()`.

Change `build()` order:

```python
self._checkout(run_id, profile, workspace)
if self._run_olddefconfig(workspace) != 0:
    raise CategorizedError(
        "make olddefconfig exited non-zero",
        category=ErrorCategory.BUILD_FAILURE,
        details={"run_id": str(run_id)},
    )
config_text = self._read_config(workspace)
```

- [ ] **Step 4: Add real olddefconfig runner**

```python
def _real_run_olddefconfig(workspace: Path) -> int:  # pragma: no cover - live_vm
    try:
        return subprocess.run(
            ["make", "-C", str(workspace), "olddefconfig"],
            timeout=_MAKE_TIMEOUT_S,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "make olddefconfig exceeded the build timeout",
            category=ErrorCategory.BUILD_FAILURE,
            details={"timeout_s": _MAKE_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise _launch_failure("make", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
```

- [ ] **Step 5: Verify**

Run:

```bash
uv run python -m pytest tests/providers/local_libvirt/test_build.py -q
```

Expected: build provider tests pass and no Kconfig prompts are possible in `runs.build`.

## Task 6: Live Smoke Check Documentation

**Files:**
- Modify: `docs/runbooks/live-stack.md`
- Optional live verification on a KVM host.

- [ ] **Step 1: Document the default demo flow**

Add a short “Kernel debugging demo smoke check” section:

```bash
set -a
. /etc/kdive/local.env
set +a
python -m kdive migrate
python -m kdive seed-demo --project demo
python -m kdive stack
```

Document these expected defaults:

- MCP URL: `http://127.0.0.1:8000/mcp`
- Kernel source: `~/src/linux` unless `KDIVE_KERNEL_SRC` is set
- Build workspace: `/var/lib/kdive/build`
- Component roots: `/var/lib/kdive/build/components:/etc/kdive/fixtures`
- Fixture catalog: `/etc/kdive/fixtures/local-libvirt`
- Fedora kdive-ready rootfs: `/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2`
- Busybox rootfs: `/var/lib/kdive/rootfs/local/busybox-bare.qcow2`

- [ ] **Step 2: Run non-live verification**

Run:

```bash
just lint
just type
uv run python -m pytest tests/scripts/test_live_stack_scripts.py tests/components/test_default_fixture_catalog.py tests/providers/local_libvirt/test_build.py -q
just lint-shell
```

Expected: all pass.

- [ ] **Step 3: Run live verification on a KVM host**

Run:

```bash
set -a
. /etc/kdive/local.env
set +a
python -m kdive migrate
python -m kdive seed-demo --project demo
python -m kdive stack
```

Then use the live-stack harness to call MCP tools for:

- `accounting.set_budget`
- `accounting.set_quota`
- `resources.list`
- `allocations.request`
- `systems.provision` with `rootfs: {"kind": "catalog", "provider": "local-libvirt", "name": "fedora-kdive-ready-43"}`
- `runs.build` with a staged `.config`
- `runs.install`
- `runs.boot`
- `artifacts.list(system_id=...)`

Expected: vulnerable kernels produce a console artifact instead of an empty `boot_timeout`;
patched kernels can boot and reach the readiness marker.

## Self-Review

- Spec coverage: stack startup defaults are covered by Tasks 1-2; rootfs catalog/defaults by
  Task 3; MCP-observable boot failures by Task 4; `olddefconfig` build behavior by Task 5;
  operator/demo documentation by Task 6.
- No rootfs MCP tool is planned; rootfs images are catalog entries and host fixture scripts.
- No containerized KDIVE server is planned; host processes remain required for libvirt path and
  permissions compatibility.
