"""CHECK<->registry parity: every resources_kind_check kind has a runtime (ADR-0071)."""

from __future__ import annotations

import re

import psycopg

from kdive.db import migrate
from kdive.domain.models import ResourceKind
from kdive.providers.composition import build_provider_resolver


def _check_allowed_kinds(conn: psycopg.Connection) -> set[str]:
    """Read the kinds admitted by the live resources_kind_check constraint."""
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'resources_kind_check'"
    ).fetchone()
    assert row is not None, "resources_kind_check constraint is missing"
    # pg renders the CHECK as ... ARRAY['local-libvirt'::text, ...]; the single-quoted
    # literals are exactly the admitted kinds (the ::text casts sit outside the quotes).
    return set(re.findall(r"'([^']+)'", row[0]))


def test_every_check_allowed_kind_has_a_registered_runtime(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    allowed = _check_allowed_kinds(pg_conn)
    assert allowed == {"local-libvirt"}  # the CHECK widen to fault-inject lands in issue 2
    resolver = build_provider_resolver()
    buildable = {k.value for k in resolver.registered_kinds()}
    # Every kind the DB will admit must resolve to a runtime (no admit-then-throw drift).
    assert allowed <= buildable
    for kind in allowed:
        assert resolver.resolve(ResourceKind(kind)) is not None


def test_every_registered_kind_is_check_allowed(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    allowed = _check_allowed_kinds(pg_conn)
    resolver = build_provider_resolver()
    # No runtime for a kind the DB forbids (discovery insert would fail otherwise).
    for kind in resolver.registered_kinds():
        assert kind.value in allowed
