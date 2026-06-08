import asyncio
import os
import sys
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


def test_local_env_defaults_are_repo_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/operator")

    env = local_env_defaults()

    # pragma: allowlist nextline secret
    assert env["KDIVE_DATABASE_URL"] == "postgresql://kdive:kdive@localhost:5432/kdive"
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


def test_seed_demo_registers_local_resource(
    monkeypatch: pytest.MonkeyPatch, migrated_url: str
) -> None:
    calls: list[str] = []

    async def fake_register(pool: object) -> None:
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
    assert all(cmd[:3] == [sys.executable, "-m", "kdive"] for cmd in commands)


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
