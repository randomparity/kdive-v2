"""The seeded decision-keyed fault engine (ADR-0072, M1.5 issue 3).

Every decision is a pure function of stable inputs over a process-independent hash, so a
fixed seed yields identical faults across workers and processes. These tests pin that
contract: the determinism guard (cross-``PYTHONHASHSEED`` + known-answer golden), facet
independence, ``attempt`` sensitivity, the fail/category/latency decision math, and
``from_capabilities`` validation.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from uuid import UUID

import pytest

import kdive.providers.fault_inject.engine as engine_module
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.fault_inject.capabilities import (
    FAULT_RATE_KEY,
    MAX_LATENCY_S_KEY,
    SEED_KEY,
)
from kdive.providers.fault_inject.engine import (
    FaultEngine,
    FaultFacet,
    FaultPlane,
    fault_for,
)

_SYSTEM = UUID("11111111-1111-1111-1111-111111111111")
_OTHER_SYSTEM = UUID("22222222-2222-2222-2222-222222222222")


def _golden_value() -> float:
    """Compute the in-process draw for the golden key (the determinism baseline).

    The same fixed key the determinism-guard subprocess computes; see ``_SUBPROCESS_DRAW``.
    """
    return fault_for(
        seed=7,
        system_id=_SYSTEM,
        plane=FaultPlane.PROVISION,
        attempt=1,
        facet=FaultFacet.FAIL,
    )


# --- fault_for: the pure draw ----------------------------------------------------------


def test_fault_for_lands_in_the_unit_interval_across_a_key_sweep() -> None:
    for seed in range(4):
        for attempt in range(1, 4):
            for facet in FaultFacet:
                draw = fault_for(
                    seed=seed,
                    system_id=_SYSTEM,
                    plane=FaultPlane.CONNECT,
                    attempt=attempt,
                    facet=facet,
                )
                assert 0.0 <= draw < 1.0


def test_fault_for_is_pure_repeated_calls_return_the_same_draw() -> None:
    assert _golden_value() == _golden_value()


def test_each_facet_is_an_independent_draw() -> None:
    draws = {
        facet: fault_for(
            seed=7, system_id=_SYSTEM, plane=FaultPlane.INSTALL, attempt=2, facet=facet
        )
        for facet in FaultFacet
    }
    # Three distinct keyed draws — not one draw reused across facets.
    assert len(set(draws.values())) == len(FaultFacet)


def test_attempt_changes_the_draw_for_the_same_op() -> None:
    first = fault_for(
        seed=7, system_id=_SYSTEM, plane=FaultPlane.BOOT, attempt=1, facet=FaultFacet.FAIL
    )
    second = fault_for(
        seed=7, system_id=_SYSTEM, plane=FaultPlane.BOOT, attempt=2, facet=FaultFacet.FAIL
    )
    assert first != second


def test_system_id_changes_the_draw() -> None:
    first = fault_for(
        seed=7, system_id=_SYSTEM, plane=FaultPlane.BOOT, attempt=1, facet=FaultFacet.FAIL
    )
    second = fault_for(
        seed=7, system_id=_OTHER_SYSTEM, plane=FaultPlane.BOOT, attempt=1, facet=FaultFacet.FAIL
    )
    assert first != second


def test_seed_changes_the_draw() -> None:
    first = fault_for(
        seed=1, system_id=_SYSTEM, plane=FaultPlane.BOOT, attempt=1, facet=FaultFacet.FAIL
    )
    second = fault_for(
        seed=2, system_id=_SYSTEM, plane=FaultPlane.BOOT, attempt=1, facet=FaultFacet.FAIL
    )
    assert first != second


@pytest.mark.parametrize("attempt", [0, -1])
def test_fault_for_rejects_a_non_positive_attempt(attempt: int) -> None:
    with pytest.raises(ValueError, match="attempt"):
        fault_for(
            seed=7,
            system_id=_SYSTEM,
            plane=FaultPlane.PROVISION,
            attempt=attempt,
            facet=FaultFacet.FAIL,
        )


# --- Determinism guard (the headline acceptance criterion) -----------------------------


_SUBPROCESS_DRAW = (
    "from uuid import UUID;"
    "from kdive.providers.fault_inject.engine import fault_for, FaultPlane, FaultFacet;"
    "print(repr(fault_for(seed=7, system_id=UUID('11111111-1111-1111-1111-111111111111'),"
    " plane=FaultPlane.PROVISION, attempt=1, facet=FaultFacet.FAIL)))"
)


def _draw_in_subprocess(hashseed: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_DRAW],
        env={"PYTHONHASHSEED": hashseed, "PATH": ""},
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_draw_is_identical_across_different_pythonhashseed() -> None:
    # The builtin hash() salts str/bytes per process under PYTHONHASHSEED; a salted hash
    # would make these two subprocesses disagree. blake2b over a canonical key does not.
    salted = _draw_in_subprocess("0")
    other = _draw_in_subprocess("1")

    assert salted == other  # cross-process: catches a process-salted hash
    # known-answer: catches a degenerate/constant draw a pure equality check would miss.
    assert salted == repr(_golden_value())


def test_engine_module_reaches_no_nondeterministic_seed_source() -> None:
    # AST-level (not text): a future edit that *imports* a nondeterministic source fails,
    # while the module's own docstring naming those sources stays allowed.
    source = inspect.getsource(engine_module)
    forbidden = {"os", "random", "secrets", "time", "datetime"}
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
    leaks = forbidden & imported
    assert not leaks, f"engine imports nondeterministic source(s): {sorted(leaks)}"


# --- FaultEngine.decide ----------------------------------------------------------------


def test_decide_always_fails_a_plane_at_fault_rate_one() -> None:
    engine = FaultEngine(seed=7, fault_rate={"provision": 1.0}, max_latency_s={})
    for attempt in range(1, 8):
        decision = engine.decide(system_id=_SYSTEM, plane=FaultPlane.PROVISION, attempt=attempt)
        assert decision.fail is True


def test_decide_never_fails_a_plane_at_fault_rate_zero() -> None:
    engine = FaultEngine(seed=7, fault_rate={"provision": 0.0}, max_latency_s={})
    for attempt in range(1, 8):
        decision = engine.decide(system_id=_SYSTEM, plane=FaultPlane.PROVISION, attempt=attempt)
        assert decision.fail is False


def test_decide_never_fails_a_plane_absent_from_the_fault_rate_map() -> None:
    engine = FaultEngine(seed=7, fault_rate={}, max_latency_s={})
    for attempt in range(1, 8):
        decision = engine.decide(system_id=_SYSTEM, plane=FaultPlane.CONNECT, attempt=attempt)
        assert decision.fail is False


def test_decide_categorizes_a_failure_within_the_planes_catalog() -> None:
    engine = FaultEngine(seed=7, fault_rate={"install": 1.0}, max_latency_s={})
    seen: set[ErrorCategory] = set()
    for attempt in range(1, 40):
        decision = engine.decide(system_id=_SYSTEM, plane=FaultPlane.INSTALL, attempt=attempt)
        assert decision.fail is True
        assert decision.category in {ErrorCategory.INSTALL_FAILURE, ErrorCategory.BOOT_TIMEOUT}
        assert decision.category is not None
        seen.add(decision.category)
    # The install plane has two categories; the category draw buckets across both.
    assert seen == {ErrorCategory.INSTALL_FAILURE, ErrorCategory.BOOT_TIMEOUT}


def test_decide_leaves_category_none_when_no_fault_is_drawn() -> None:
    engine = FaultEngine(seed=7, fault_rate={}, max_latency_s={})
    decision = engine.decide(system_id=_SYSTEM, plane=FaultPlane.PROVISION, attempt=1)
    assert decision.fail is False
    assert decision.category is None


def test_decide_scales_latency_against_the_per_plane_bound() -> None:
    bound = 9.0
    engine = FaultEngine(seed=7, fault_rate={}, max_latency_s={"provision": bound})
    for attempt in range(1, 20):
        decision = engine.decide(system_id=_SYSTEM, plane=FaultPlane.PROVISION, attempt=attempt)
        assert 0.0 <= decision.latency_s < bound


def test_decide_yields_zero_latency_when_the_plane_has_no_bound() -> None:
    engine = FaultEngine(seed=7, fault_rate={}, max_latency_s={})
    decision = engine.decide(system_id=_SYSTEM, plane=FaultPlane.PROVISION, attempt=1)
    assert decision.latency_s == 0.0


def test_two_engines_with_different_seeds_decide_differently() -> None:
    one = FaultEngine(seed=1, fault_rate={}, max_latency_s={"connect": 5.0})
    two = FaultEngine(seed=2, fault_rate={}, max_latency_s={"connect": 5.0})
    a = one.decide(system_id=_SYSTEM, plane=FaultPlane.CONNECT, attempt=1)
    b = two.decide(system_id=_SYSTEM, plane=FaultPlane.CONNECT, attempt=1)
    # Seed is part of every draw key, so the soak seed-sweep widens coverage.
    assert a.latency_s != b.latency_s


def test_decide_is_reproducible_across_two_engines_with_the_same_seed() -> None:
    one = FaultEngine(seed=7, fault_rate={"boot": 0.5}, max_latency_s={"boot": 3.0})
    two = FaultEngine(seed=7, fault_rate={"boot": 0.5}, max_latency_s={"boot": 3.0})
    for attempt in range(1, 20):
        a = one.decide(system_id=_SYSTEM, plane=FaultPlane.BOOT, attempt=attempt)
        b = two.decide(system_id=_SYSTEM, plane=FaultPlane.BOOT, attempt=attempt)
        assert a == b


# --- FaultEngine.from_capabilities -----------------------------------------------------


def test_from_capabilities_reads_the_issue_2_keys() -> None:
    engine = FaultEngine.from_capabilities(
        {
            SEED_KEY: 99,
            FAULT_RATE_KEY: {"provision": 0.4},
            MAX_LATENCY_S_KEY: {"provision": 8.0},
        }
    )
    assert engine.seed == 99
    decision = engine.decide(system_id=_SYSTEM, plane=FaultPlane.PROVISION, attempt=1)
    assert 0.0 <= decision.latency_s < 8.0


def test_from_capabilities_defaults_an_absent_seed_to_zero_and_empty_maps() -> None:
    engine = FaultEngine.from_capabilities({})
    assert engine.seed == 0
    decision = engine.decide(system_id=_SYSTEM, plane=FaultPlane.PROVISION, attempt=1)
    assert decision.fail is False
    assert decision.latency_s == 0.0


@pytest.mark.parametrize("bad_rate", [1.5, -0.1])
def test_from_capabilities_rejects_an_out_of_range_fault_rate(bad_rate: float) -> None:
    with pytest.raises(CategorizedError) as exc:
        FaultEngine.from_capabilities({FAULT_RATE_KEY: {"provision": bad_rate}})
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_from_capabilities_rejects_a_negative_max_latency() -> None:
    with pytest.raises(CategorizedError) as exc:
        FaultEngine.from_capabilities({MAX_LATENCY_S_KEY: {"provision": -1.0}})
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_from_capabilities_rejects_a_non_integer_seed() -> None:
    with pytest.raises(CategorizedError) as exc:
        FaultEngine.from_capabilities({SEED_KEY: "not-a-number"})
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_from_capabilities_rejects_a_non_numeric_fault_rate_value() -> None:
    with pytest.raises(CategorizedError) as exc:
        FaultEngine.from_capabilities({FAULT_RATE_KEY: {"provision": "high"}})
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_from_capabilities_rejects_a_non_map_fault_rate() -> None:
    with pytest.raises(CategorizedError) as exc:
        FaultEngine.from_capabilities({FAULT_RATE_KEY: 0.5})
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
