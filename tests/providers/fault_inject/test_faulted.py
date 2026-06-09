"""Tests for the faulting wrapper that threads the seeded engine into the mock ports.

ADR-0074: a thin `FaultedProvision` / `FaultedInstall` consults a `FaultEngine` before
delegating to the happy-path port — a drawn `fail` raises `CategorizedError(category)`, a
drawn `latency` blocks the (sync) port via an injected `sleep_s` seam, and `attempt` is a
caller-supplied durable input (default 1), never a port-held counter.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.fault_inject.engine import FaultEngine, FaultPlane
from kdive.providers.fault_inject.faulted import FaultedInstall, FaultedProvision
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.fault_inject.provider import FaultInjectInstall, FaultInjectProvision

_SYSTEM = UUID("00000000-0000-0000-0000-0000000000aa")
_RUN = UUID("00000000-0000-0000-0000-0000000000bb")


def _noop_sleep(_delay: float) -> None:
    return None


def _provision(
    engine: FaultEngine,
    *,
    attempt_for: Callable[[UUID], int] = lambda _sid: 1,
    sleep_s: Callable[[float], None] = _noop_sleep,
) -> FaultedProvision:
    inventory = FaultInjectInventory()
    return FaultedProvision(
        FaultInjectProvision(inventory), engine, attempt_for=attempt_for, sleep_s=sleep_s
    )


def _seed_that_fails(plane: FaultPlane) -> FaultEngine:
    """An engine certain to draw a failure for ``plane`` (fault_rate 1.0)."""
    return FaultEngine(seed=7, fault_rate={plane.value: 1.0}, max_latency_s={})


def _seed_that_never_fails(plane: FaultPlane, *, max_latency_s: float = 0.0) -> FaultEngine:
    return FaultEngine(
        seed=7, fault_rate={plane.value: 0.0}, max_latency_s={plane.value: max_latency_s}
    )


def test_provision_fail_draw_raises_categorized_error_with_catalog_category() -> None:
    engine = _seed_that_fails(FaultPlane.PROVISION)
    wrapper = _provision(engine)
    with pytest.raises(CategorizedError) as exc:
        wrapper.provision(_SYSTEM, object())
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_provision_no_fail_draw_delegates_and_returns_synthetic_domain() -> None:
    engine = _seed_that_never_fails(FaultPlane.PROVISION)
    wrapper = _provision(engine)
    domain = wrapper.provision(_SYSTEM, object())
    assert domain == f"fault-inject-{_SYSTEM}"


def test_provision_latency_sleeps_for_the_engine_computed_delay() -> None:
    engine = _seed_that_never_fails(FaultPlane.PROVISION, max_latency_s=1000.0)
    recorded: list[float] = []
    wrapper = _provision(engine, sleep_s=recorded.append)
    wrapper.provision(_SYSTEM, object())
    expected = engine.decide(system_id=_SYSTEM, plane=FaultPlane.PROVISION, attempt=1).latency_s
    assert recorded == [expected]
    assert expected > 0.0  # a real seed-derived delay, not a no-op


def test_provision_absent_plane_config_neither_sleeps_nor_raises() -> None:
    engine = FaultEngine(seed=7, fault_rate={}, max_latency_s={})
    recorded: list[float] = []
    wrapper = _provision(engine, sleep_s=recorded.append)
    domain = wrapper.provision(_SYSTEM, object())
    assert domain == f"fault-inject-{_SYSTEM}"
    assert recorded == []  # absent plane => zero latency => no sleep call


def test_attempt_for_is_threaded_into_the_draw() -> None:
    engine = _seed_that_never_fails(FaultPlane.PROVISION, max_latency_s=1000.0)
    first: list[float] = []
    second: list[float] = []
    _provision(engine, attempt_for=lambda _sid: 1, sleep_s=first.append).provision(
        _SYSTEM, object()
    )
    _provision(engine, attempt_for=lambda _sid: 2, sleep_s=second.append).provision(
        _SYSTEM, object()
    )
    assert first != second  # a different durable attempt yields a different latency draw


def test_zero_latency_does_not_call_sleep() -> None:
    engine = _seed_that_never_fails(FaultPlane.PROVISION, max_latency_s=0.0)
    recorded: list[float] = []
    _provision(engine, sleep_s=recorded.append).provision(_SYSTEM, object())
    assert recorded == []


def test_teardown_and_reprovision_delegate_unchanged() -> None:
    engine = _seed_that_fails(FaultPlane.PROVISION)
    inventory = FaultInjectInventory()
    inner = FaultInjectProvision(inventory)
    wrapper = FaultedProvision(inner, engine, sleep_s=lambda _s: None)
    # teardown never draws a fault (it is a compensation, not a perturbed op).
    wrapper.teardown("fault-inject-x")
    # reprovision draws on the provision plane; a fail-certain engine raises.
    with pytest.raises(CategorizedError):
        wrapper.reprovision(_SYSTEM, object())


def test_install_fail_draw_raises_categorized_error() -> None:
    engine = _seed_that_fails(FaultPlane.INSTALL)
    wrapper = FaultedInstall(FaultInjectInstall(), engine, sleep_s=lambda _s: None)
    with pytest.raises(CategorizedError) as exc:
        wrapper.install(_SYSTEM, _RUN, "kernel-ref", cmdline="console=ttyS0")
    assert exc.value.category in {ErrorCategory.INSTALL_FAILURE, ErrorCategory.BOOT_TIMEOUT}


def test_install_latency_sleeps_for_the_engine_computed_delay() -> None:
    engine = _seed_that_never_fails(FaultPlane.INSTALL, max_latency_s=1000.0)
    recorded: list[float] = []
    wrapper = FaultedInstall(FaultInjectInstall(), engine, sleep_s=recorded.append)
    wrapper.install(_SYSTEM, _RUN, "kernel-ref", cmdline="console=ttyS0")
    expected = engine.decide(system_id=_SYSTEM, plane=FaultPlane.INSTALL, attempt=1).latency_s
    assert recorded == [expected]
    assert expected > 0.0


def test_boot_uses_the_boot_plane() -> None:
    engine = _seed_that_fails(FaultPlane.BOOT)
    wrapper = FaultedInstall(FaultInjectInstall(), engine, sleep_s=lambda _s: None)
    with pytest.raises(CategorizedError) as exc:
        wrapper.boot(_SYSTEM)
    assert exc.value.category in {ErrorCategory.READINESS_FAILURE, ErrorCategory.BOOT_TIMEOUT}


def test_fresh_system_id_each_call_is_independent() -> None:
    engine = _seed_that_never_fails(FaultPlane.PROVISION, max_latency_s=1000.0)
    a: list[float] = []
    b: list[float] = []
    sid_a, sid_b = uuid4(), uuid4()
    _provision(engine, sleep_s=a.append).provision(sid_a, object())
    _provision(engine, sleep_s=b.append).provision(sid_b, object())
    assert a != b  # the draw is keyed on system_id
