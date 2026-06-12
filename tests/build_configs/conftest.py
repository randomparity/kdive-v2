"""Fixtures for build-config catalog and seed unit tests (ADR-0096)."""

from __future__ import annotations

from typing import Any

import pytest

from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import StoredArtifact


class _FakeCursor:
    """Fake async cursor: returns one row from existing_sha or None."""

    def __init__(self, existing_sha: dict[str, str], upserted_rows: dict[str, Any]) -> None:
        self._existing_sha = existing_sha
        self._upserted_rows = upserted_rows
        self._last_name: str | None = None
        self._is_select = True

    async def execute(self, query: str, params: dict[str, Any]) -> None:
        name = params.get("name")
        self._last_name = name
        # Distinguish SELECT from INSERT based on query text.
        self._is_select = query.strip().upper().startswith("SELECT")
        if not self._is_select and name is not None:
            # Capture the upserted row.
            self._upserted_rows[name] = {k: v for k, v in params.items() if k != "name"}
            self._upserted_rows[name]["name"] = name

    async def fetchone(self) -> dict[str, Any] | None:
        if not self._is_select or self._last_name is None:
            return None
        sha = self._existing_sha.get(self._last_name)
        if sha is None:
            return None
        return {"sha256": sha}

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass


class FakeConn:
    """Minimal async-connection double for seed unit tests."""

    def __init__(self) -> None:
        self.existing_sha: dict[str, str] = {}
        self.upserted_rows: dict[str, Any] = {}

    def cursor(self, **_kwargs: Any) -> _FakeCursor:
        return _FakeCursor(self.existing_sha, self.upserted_rows)


class FakeStore:
    """Minimal object-store double: records keys of put_artifact calls."""

    def __init__(self) -> None:
        self.put_keys: list[str] = []

    def put_artifact(self, request: Any) -> StoredArtifact:
        key = request.key()
        self.put_keys.append(key)
        return StoredArtifact(
            key=key,
            etag="fake-etag",
            sensitivity=Sensitivity.REDACTED,
            retention_class="build-config",
        )


@pytest.fixture
def fake_conn() -> FakeConn:
    """A lightweight async-connection double for seed unit tests."""
    return FakeConn()


@pytest.fixture
def fake_store() -> FakeStore:
    """A lightweight object-store double for seed unit tests."""
    return FakeStore()
