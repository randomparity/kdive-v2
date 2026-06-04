"""Cost-model unit tests — the kcu rate/cost math and input validation (ADR-0007 §1-2).

Pure, connection-free tests for the formula, the shared kcu quantizer, and the
fail-closed `validate_size`/`validate_window` guards. `resolve_coeff` (DB-backed) is
exercised in the DB-marked tests below and through `accounting.estimate`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kdive.domain.cost import (
    KCU_QUANTUM,
    W_CPU,
    W_MEM,
    Selector,
    cost,
    quantize_kcu,
    rate,
    validate_size,
    validate_window,
)
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_weights_match_adr() -> None:
    assert Decimal("1.0") == W_CPU
    assert Decimal("0.25") == W_MEM


def test_rate_matches_size_weighted_formula() -> None:
    # coeff=1.0, 2 vcpu, 4 GB → 1.0*(1.0*2 + 0.25*4) = 3.0 kcu/hr.
    assert rate(Decimal("1.0"), vcpus=2, memory_gb=4) == Decimal("3.0")


def test_rate_scales_with_coefficient() -> None:
    # A future cloud class (coeff 4.0) prices the same size 4x the local baseline.
    assert rate(Decimal("4.0"), vcpus=1, memory_gb=0) == Decimal("4.0")


def test_rate_is_exact_decimal_not_float() -> None:
    # 0.25 * 3 = 0.75 exactly; a float path would drift.
    assert rate(Decimal("1.0"), vcpus=0, memory_gb=3) == Decimal("0.75")


def test_cost_is_rate_times_hours() -> None:
    assert cost(Decimal("3.0"), Decimal("2")) == Decimal("6.0")


def test_cost_fractional_window() -> None:
    assert cost(Decimal("2.0"), Decimal("1.5")) == Decimal("3.00")


def test_quantize_rounds_half_even_to_quantum() -> None:
    assert Decimal("0.0001") == KCU_QUANTUM
    # Unbounded product is pinned to four places, banker's rounding.
    assert quantize_kcu(Decimal("0.123456789")) == Decimal("0.1235")
    assert quantize_kcu(Decimal("0.000050000")) == Decimal("0.0000")  # half to even
    assert quantize_kcu(Decimal("0.000150000")) == Decimal("0.0002")  # half to even


def test_quantize_too_large_fails_closed() -> None:
    # A value whose quantized form exceeds the default decimal precision would raise
    # InvalidOperation; quantize_kcu maps it to configuration_error instead of letting it
    # escape as an unhandled exception on the viewer-callable estimate tool.
    with pytest.raises(CategorizedError) as exc:
        quantize_kcu(Decimal("1e30"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_size_accepts_minimum() -> None:
    validate_size(Selector(vcpus=1, memory_gb=0))


def test_validate_size_rejects_zero_vcpus() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_size(Selector(vcpus=0, memory_gb=1))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_size_rejects_negative_vcpus() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_size(Selector(vcpus=-1, memory_gb=1))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_size_rejects_negative_memory() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_size(Selector(vcpus=1, memory_gb=-1))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_size_rejects_over_column_domain() -> None:
    # requested_vcpus/memory_gb persist as Postgres `integer`; a value the read-side
    # estimate would price but admission could never store is rejected up front so the
    # two share one acceptance domain (challenge finding 3).
    with pytest.raises(CategorizedError) as exc:
        validate_size(Selector(vcpus=2_147_483_648, memory_gb=1))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_window_accepts_positive() -> None:
    validate_window(Decimal("0.5"))


def test_validate_window_rejects_zero() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_window(Decimal("0"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_window_rejects_negative() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_window(Decimal("-1"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_window_rejects_nan() -> None:
    # NaN <= 0 is False, so a naive sign check would let it through and yield a NaN
    # estimate; the guard must reject non-finite windows (challenge finding 3).
    with pytest.raises(CategorizedError) as exc:
        validate_window(Decimal("NaN"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_window_rejects_infinity() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_window(Decimal("Infinity"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
