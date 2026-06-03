"""Tests for CapabilityRegistry.register / dispatch (ADR-0022, issue #13)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.state import ResourceStatus
from kdive.providers.capability import (
    CapabilityRegistry,
    CleanupGuarantee,
    OpContract,
    Plane,
)
from tests.providers.conftest import (
    LIBVIRT,
    FakeProvider,
    UnhonoredProvider,
    build_capability,
)


def _registry_with_one_build_provider() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(
        FakeProvider(),
        [build_capability()],
        provider_id="p-1",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )
    return registry


def _register(
    registry: CapabilityRegistry,
    provider_id: str,
    *,
    health: ResourceStatus = ResourceStatus.AVAILABLE,
    cost_class: str = "standard",
) -> None:
    registry.register(
        FakeProvider(),
        [build_capability()],
        provider_id=provider_id,
        health=health,
        cost_class=cost_class,
    )


# --- register ---------------------------------------------------------------


def test_register_then_dispatch_returns_bound_op() -> None:
    registry = _registry_with_one_build_provider()
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-1"
    assert bound.operation == "build"
    assert bound.contract.long_running is True
    assert str(bound.call(None, {})) == "kernel-1"


def test_empty_provider_id_raises_value_error() -> None:
    registry = CapabilityRegistry()
    with pytest.raises(ValueError, match="provider_id"):
        registry.register(
            FakeProvider(),
            [build_capability()],
            provider_id="",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )


def test_duplicate_provider_id_raises_value_error() -> None:
    registry = _registry_with_one_build_provider()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            FakeProvider(),
            [build_capability(operation="install", plane=Plane.INSTALL)],
            provider_id="p-1",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )


def test_same_key_twice_in_one_call_raises_and_registers_nothing() -> None:
    registry = CapabilityRegistry()
    with pytest.raises(ValueError, match="twice"):
        registry.register(
            FakeProvider(),
            [build_capability(), build_capability()],
            provider_id="p-1",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )
    # Atomic: nothing registered, id still free.
    with pytest.raises(CategorizedError):
        registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    registry.register(
        FakeProvider(),
        [build_capability()],
        provider_id="p-1",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )


def test_unhonored_capability_raises_not_implemented_at_register() -> None:
    registry = CapabilityRegistry()
    with pytest.raises(CategorizedError) as exc:
        registry.register(
            UnhonoredProvider(),
            [build_capability()],
            provider_id="p-1",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )
    assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED


def test_register_is_atomic_on_partial_failure() -> None:
    registry = CapabilityRegistry()
    # First cap honored (FakeProvider.build exists), second unhonored (no ghost_op).
    with pytest.raises(CategorizedError):
        registry.register(
            FakeProvider(),
            [build_capability(), build_capability(operation="ghost_op")],
            provider_id="p-1",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )
    # The honored 'build' cap must NOT have been recorded.
    with pytest.raises(CategorizedError):
        registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    # And the id is free for a corrected retry.
    registry.register(
        FakeProvider(),
        [build_capability()],
        provider_id="p-1",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )


def test_contract_divergence_across_providers_raises() -> None:
    registry = _registry_with_one_build_provider()
    diverging = OpContract(
        idempotent=True,
        destructive=True,  # differs from DEFAULT_CONTRACT.destructive
        cancelable=False,
        long_running=True,
        cleanup=CleanupGuarantee.BEST_EFFORT,
    )
    with pytest.raises(ValueError, match="contract"):
        registry.register(
            FakeProvider(),
            [build_capability(contract=diverging)],
            provider_id="p-2",
            health=ResourceStatus.AVAILABLE,
            cost_class="standard",
        )


def test_equal_contract_second_provider_registers() -> None:
    registry = _registry_with_one_build_provider()
    registry.register(
        FakeProvider(),
        [build_capability()],  # equal contract
        provider_id="p-2",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )
    # Two candidates now under the same key; dispatch resolves deterministically.
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-1"  # provider_id tiebreak


# --- dispatch ---------------------------------------------------------------


def test_dispatch_unregistered_op_raises_not_implemented_with_key() -> None:
    registry = CapabilityRegistry()
    with pytest.raises(CategorizedError) as exc:
        registry.dispatch(Plane.CONTROL, "force_crash", LIBVIRT)
    assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED
    assert exc.value.details["operation"] == "force_crash"
    assert exc.value.details["plane"] == Plane.CONTROL
    assert exc.value.details["resource_kind"] == LIBVIRT


def test_pin_wins_over_a_healthier_rival() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a", health=ResourceStatus.AVAILABLE)
    _register(registry, "p-b", health=ResourceStatus.DEGRADED)
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT, pin="p-b")
    assert bound.provider_id == "p-b"


def test_pin_to_non_advertising_provider_raises_not_implemented() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a")
    with pytest.raises(CategorizedError) as exc:
        registry.dispatch(Plane.BUILD, "build", LIBVIRT, pin="ghost")
    assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED
    assert exc.value.details["pin"] == "ghost"


def test_health_beats_cost_class() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a", health=ResourceStatus.DEGRADED, cost_class="aaa")
    _register(registry, "p-b", health=ResourceStatus.AVAILABLE, cost_class="zzz")
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-b"  # healthier wins despite worse cost_class


def test_cost_class_beats_provider_id() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a", cost_class="zzz")
    _register(registry, "p-b", cost_class="aaa")
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-b"  # cheaper cost_class wins despite later id


def test_provider_id_is_the_final_tiebreak() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-b")
    _register(registry, "p-a")
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-a"  # equal health+cost → lowest id


def test_health_never_filters_offline_only_candidate() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a", health=ResourceStatus.OFFLINE)
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-a"  # offline still dispatches


def test_degraded_beats_offline() -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a", health=ResourceStatus.OFFLINE)
    _register(registry, "p-b", health=ResourceStatus.DEGRADED)
    bound = registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert bound.provider_id == "p-b"


def test_partial_provider_dispatches_advertised_only() -> None:
    from tests.providers.conftest import PartialFakeProvider

    registry = CapabilityRegistry()
    registry.register(
        PartialFakeProvider(),
        [build_capability()],
        provider_id="p-1",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )
    assert registry.dispatch(Plane.BUILD, "build", LIBVIRT).provider_id == "p-1"
    with pytest.raises(CategorizedError) as exc:
        registry.dispatch(Plane.CONTROL, "force_crash", LIBVIRT)
    assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED


def test_unhonored_at_dispatch_raises_not_implemented() -> None:
    from tests.providers.conftest import MutableProvider

    registry = CapabilityRegistry()
    provider = MutableProvider()
    registry.register(
        provider,
        [build_capability()],
        provider_id="p-1",
        health=ResourceStatus.AVAILABLE,
        cost_class="standard",
    )
    del provider.build  # drop the method after registration
    with pytest.raises(CategorizedError) as exc:
        registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert exc.value.category is ErrorCategory.NOT_IMPLEMENTED


def test_dispatch_logs_the_selection(caplog: pytest.LogCaptureFixture) -> None:
    registry = CapabilityRegistry()
    _register(registry, "p-a")
    _register(registry, "p-b")
    with caplog.at_level("DEBUG", logger="kdive.providers.capability"):
        registry.dispatch(Plane.BUILD, "build", LIBVIRT)
    assert any("p-a" in record.getMessage() for record in caplog.records)
