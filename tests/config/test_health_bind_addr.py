"""The aux-listener bind address is a config key with a loopback default (ADR-0090 §5).

The aux listener is an operational surface, not a public one; the network boundary is
its access control. The bind address therefore defaults to loopback so widening it is an
explicit, reviewed config act rather than implementation memory.
"""

from __future__ import annotations

import kdive.config as config
from kdive.config.core_settings import HEALTH_BIND_ADDR
from kdive.health.aux_bind import resolve_health_bind


def test_default_binds_loopback() -> None:
    config.load({})
    assert config.get(HEALTH_BIND_ADDR) == "127.0.0.1:9464"


def test_resolve_splits_host_and_port() -> None:
    config.load({})
    host, port = resolve_health_bind()
    assert host == "127.0.0.1"
    assert port == 9464


def test_resolve_honors_override() -> None:
    config.load({"KDIVE_HEALTH_BIND_ADDR": "0.0.0.0:8081"})
    host, port = resolve_health_bind()
    assert host == "0.0.0.0"
    assert port == 8081


def test_setting_is_registered() -> None:
    names = {s.name for s in config.all_settings()}
    assert "KDIVE_HEALTH_BIND_ADDR" in names


def test_per_process_default_ports_are_distinct() -> None:
    """Three processes on one host must not collide on the default aux port (ADR-0090 §5)."""
    config.load({})
    server = resolve_health_bind("server")
    worker = resolve_health_bind("worker")
    reconciler = resolve_health_bind("reconciler")
    assert server == ("127.0.0.1", 9464)
    assert worker == ("127.0.0.1", 9465)
    assert reconciler == ("127.0.0.1", 9466)


def test_explicit_override_wins_for_every_process() -> None:
    """An explicit KDIVE_HEALTH_BIND_ADDR is the single source of truth, per process."""
    config.load({"KDIVE_HEALTH_BIND_ADDR": "0.0.0.0:7000"})
    assert resolve_health_bind("worker") == ("0.0.0.0", 7000)
    assert resolve_health_bind("reconciler") == ("0.0.0.0", 7000)
    assert resolve_health_bind("server") == ("0.0.0.0", 7000)


def test_default_process_is_server() -> None:
    config.load({})
    assert resolve_health_bind() == resolve_health_bind("server")
