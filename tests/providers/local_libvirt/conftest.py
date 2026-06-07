"""Fixture registration for local-libvirt provider tests."""

from __future__ import annotations

from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401
from tests.providers.local_libvirt.fakes import (  # noqa: F401
    FakeDomain,
    FakeLibvirtConn,
    libvirt_error,
)
