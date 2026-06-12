import asyncio
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from kdive.admin.bootstrap import (
    default_fixture_files,
    install_fixtures,
    migrate,
    seed_demo,
    seed_project_statements,
)
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact


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


def test_migrate_seeds_baseline_rootfs_idempotently(
    monkeypatch: pytest.MonkeyPatch, postgres_url: str
) -> None:
    # migrate applies the schema then seeds the packaged baseline as `defined` rows
    # (deploy ordering migrate → seed); a re-run adds no rows.
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    monkeypatch.delenv("KDIVE_FIXTURE_CATALOG_PATH", raising=False)
    migrate(postgres_url)
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        first = conn.execute(
            "SELECT count(*) FROM image_catalog WHERE state = 'defined'"
        ).fetchone()
    assert first is not None and first[0] >= 1

    migrate(postgres_url)
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        second = conn.execute("SELECT count(*) FROM image_catalog").fetchone()
    assert second is not None and second[0] == first[0]


def test_migrate_without_s3_skips_build_config_seed(
    monkeypatch: pytest.MonkeyPatch, postgres_url: str
) -> None:
    # No KDIVE_S3_* configured: migrate still applies the schema and seeds the baseline
    # rootfs, but the object-store-backed build-config seed is skipped cleanly (ADR-0096).
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for var in ("KDIVE_S3_ENDPOINT_URL", "KDIVE_S3_BUCKET", "KDIVE_S3_REGION"):
        monkeypatch.delenv(var, raising=False)

    migrate(postgres_url)

    with psycopg.connect(postgres_url, autocommit=True) as conn:
        rootfs = conn.execute(
            "SELECT count(*) FROM image_catalog WHERE state = 'defined'"
        ).fetchone()
        configs = conn.execute("SELECT count(*) FROM build_config_catalog").fetchone()
    assert rootfs is not None and rootfs[0] >= 1
    assert configs is not None and configs[0] == 0


class _FakeStore:
    """Object-store double for the build-config seed (it only writes bytes via put_artifact)."""

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        return StoredArtifact(
            key=request.key(),
            etag="fake-etag",
            sensitivity=Sensitivity.REDACTED,
            retention_class="build-config",
        )


def test_migrate_with_s3_seeds_build_config(
    monkeypatch: pytest.MonkeyPatch, postgres_url: str
) -> None:
    # With an object store available, migrate seeds the packaged kdump fragment row.
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", lambda: _FakeStore())

    migrate(postgres_url)

    with psycopg.connect(postgres_url, autocommit=True) as conn:
        row = conn.execute("SELECT name FROM build_config_catalog WHERE name = 'kdump'").fetchone()
    assert row is not None and row[0] == "kdump"


def test_install_fixtures_refuses_overwrite_without_force(tmp_path: Path) -> None:
    fixture_dest = tmp_path / "fixtures"
    (fixture_dest / "manifest.yaml").parent.mkdir(parents=True)
    (fixture_dest / "manifest.yaml").write_text("custom", encoding="utf-8")

    with pytest.raises(FileExistsError):
        install_fixtures(fixture_dest)

    install_fixtures(fixture_dest, force=True)
