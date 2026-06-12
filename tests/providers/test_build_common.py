"""Tests for the shared kernel-build fragment helpers (ADR-0096)."""

from __future__ import annotations

import hashlib
from types import TracebackType

import pytest

from kdive.build_configs import defaults as build_defaults
from kdive.build_configs.catalog import BuildConfigEntry
from kdive.build_configs.defaults import DEFAULT_CONFIG_REF, build_config_fetch_from_env
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import FetchedArtifact
from kdive.provider_components.references import CatalogComponentRef
from kdive.providers.build_common import (
    _dropped_fragment_symbols,
    _fragment_symbols,
)


def test_fragment_symbols_keeps_y_and_m_drops_comments_and_unset() -> None:
    fragment = (
        "CONFIG_CRASH_DUMP=y\n"
        "CONFIG_FOO=m\n"
        "# CONFIG_BAR is not set\n"
        "\n"
        "CONFIG_BAZ=n\n"
        "CONFIG_QUX=128\n"
    )
    assert _fragment_symbols(fragment) == ["CONFIG_CRASH_DUMP", "CONFIG_FOO"]


def test_dropped_fragment_symbols_reports_a_dropped_option() -> None:
    fragment = "CONFIG_CRASH_DUMP=y\nCONFIG_PROC_VMCORE=y\n# a comment\n"
    final = "CONFIG_CRASH_DUMP=y\n# CONFIG_PROC_VMCORE is not set\n"
    assert _dropped_fragment_symbols(fragment, final) == ["CONFIG_PROC_VMCORE"]


def test_dropped_fragment_symbols_empty_when_all_survive() -> None:
    fragment = "CONFIG_CRASH_DUMP=y\n"
    final = "CONFIG_CRASH_DUMP=y\nCONFIG_OTHER=y\n"
    assert _dropped_fragment_symbols(fragment, final) == []


def test_dropped_fragment_symbols_accepts_module_survivor() -> None:
    fragment = "CONFIG_FOO=m\n"
    final = "CONFIG_FOO=m\n"
    assert _dropped_fragment_symbols(fragment, final) == []


def test_default_config_ref_is_the_kdump_catalog_entry() -> None:
    assert (
        CatalogComponentRef(kind="catalog", provider="system", name="kdump") == DEFAULT_CONFIG_REF
    )


# --- build_config_fetch_from_env wrapper ---------------------------------------------


class _FakeConn:
    """A sync-connection double that records whether it was closed (the per-build leak guard)."""

    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.closed = True


def _patch_fetch_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    conn: _FakeConn,
    entry: BuildConfigEntry | None,
    store: object,
) -> None:
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://stub/stub")
    monkeypatch.setattr(build_defaults.psycopg, "connect", lambda _url: conn)
    monkeypatch.setattr(build_defaults, "get_build_config_sync", lambda _conn, _name: entry)
    monkeypatch.setattr(build_defaults, "object_store_from_env", lambda: store)


def test_build_config_fetch_unknown_name_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConn()
    _patch_fetch_env(monkeypatch, conn=conn, entry=None, store=object())

    with pytest.raises(CategorizedError) as caught:
        build_config_fetch_from_env()("nope")

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert conn.closed  # the connection is released even on the not-found branch


def test_build_config_fetch_returns_verified_bytes_and_closes_conn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"CONFIG_CRASH_DUMP=y\n"
    entry = BuildConfigEntry(
        name="kdump",
        object_key="system/build-configs/kdump/kdump.config",
        sha256=hashlib.sha256(data).hexdigest(),
        description="",
    )

    class _FakeStore:
        def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
            assert key == entry.object_key
            assert etag is None
            return FetchedArtifact(data, Sensitivity.REDACTED, "build-config")

    conn = _FakeConn()
    _patch_fetch_env(monkeypatch, conn=conn, entry=entry, store=_FakeStore())

    assert build_config_fetch_from_env()("kdump") == data
    assert conn.closed  # the sync connection is released after the fetch (leak guard)
