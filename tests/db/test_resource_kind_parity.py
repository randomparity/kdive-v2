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


def test_check_admits_local_libvirt_and_fault_inject(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)

    # Migration 0018 widens the CHECK to admit fault-inject alongside local-libvirt.
    assert _check_allowed_kinds(pg_conn) == {"local-libvirt", "fault-inject"}


def test_every_check_allowed_kind_has_a_buildable_runtime(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    allowed = _check_allowed_kinds(pg_conn)

    # Parity is "every admitted kind can be built", not "default prod registers it":
    # fault-inject is opt-in, so the fully-enabled resolver is the buildable universe.
    resolver = build_provider_resolver(enable_fault_inject=True)
    buildable = {k.value for k in resolver.registered_kinds()}
    assert allowed <= buildable  # no admit-then-throw drift
    for kind in allowed:
        assert resolver.resolve(ResourceKind(kind)) is not None


def test_every_registered_kind_is_check_allowed(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    allowed = _check_allowed_kinds(pg_conn)

    # No runtime for a kind the DB forbids (discovery insert would fail otherwise) —
    # checked for the widest registry, which includes the opt-in fault-inject runtime.
    resolver = build_provider_resolver(enable_fault_inject=True)
    for kind in resolver.registered_kinds():
        assert kind.value in allowed


def test_default_production_registry_registers_only_local_libvirt(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)

    # The CHECK admits fault-inject, but the default (opt-in off) registry must not
    # register it — a default production deployment has no bookable fault-inject Resource.
    resolver = build_provider_resolver(enable_fault_inject=False)
    assert resolver.registered_kinds() == frozenset({ResourceKind.LOCAL_LIBVIRT})
