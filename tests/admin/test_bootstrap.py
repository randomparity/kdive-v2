import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from kdive.admin.bootstrap import (
    default_fixture_files,
    install_fixtures,
    seed_demo,
    seed_project_statements,
)


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


def test_default_fixture_files_include_catalog() -> None:
    fixture_files = default_fixture_files()

    assert "manifest.yaml" in fixture_files
    assert "profiles/console-ready_x86_64.yaml" in fixture_files


def test_install_fixtures_refuses_overwrite_without_force(tmp_path: Path) -> None:
    fixture_dest = tmp_path / "fixtures"
    (fixture_dest / "manifest.yaml").parent.mkdir(parents=True)
    (fixture_dest / "manifest.yaml").write_text("custom", encoding="utf-8")

    with pytest.raises(FileExistsError):
        install_fixtures(fixture_dest)

    install_fixtures(fixture_dest, force=True)
