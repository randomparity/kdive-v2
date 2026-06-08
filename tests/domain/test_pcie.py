"""Tests for the reusable PCIe match-spec matcher (ADR-0068, issue #158).

The matcher functions are driven directly with injected descriptor lists and active-claim
sets — no host, no provider. Config-vs-capacity is read from the RETURN VALUE; malformed
grammar raises a ``CategorizedError(CONFIGURATION_ERROR)`` and never an uncaught exception.
"""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.pcie import (
    MatchOutcome,
    PCIeClaim,
    PCIeDescriptor,
    descriptor_matches,
    parse_match_spec,
    resolve_multiset,
    resolve_spec,
)


def _desc(bdf: str, vendor: str, device: str, cls: str, label: str = "card") -> PCIeDescriptor:
    return {
        "bdf": bdf,
        "vendor_id": vendor,
        "device_id": device,
        "class_code": cls,
        "label": label,
    }


def _claim(bdf: str, vendor: str = "8086", device: str = "1572") -> PCIeClaim:
    return {"bdf": bdf, "vendor_id": vendor, "device_id": device}


# Two Intel X710 NICs (8086:1572, class 020000) and one NVMe (1234:5678, class 010802).
X710_A = _desc("0000:3b:00.0", "8086", "1572", "020000", "Intel X710")
X710_B = _desc("0000:3b:00.1", "8086", "1572", "020000", "Intel X710")
NVME = _desc("0000:5e:00.0", "1234", "5678", "010802", "Acme NVMe")
FLEET = [X710_A, X710_B, NVME]


# --- grammar parsing -------------------------------------------------------------------


def test_parse_vendor_device_exact() -> None:
    spec = parse_match_spec("8086:1572")
    assert descriptor_matches(spec, X710_A)
    assert not descriptor_matches(spec, NVME)


def test_parse_class_high_byte_two_hex() -> None:
    spec = parse_match_spec("class=02")
    assert descriptor_matches(spec, X710_A)  # 020000 high byte 02
    assert not descriptor_matches(spec, NVME)  # 010802 high byte 01


def test_parse_class_four_hex_exact_subclass() -> None:
    spec = parse_match_spec("class=0200")
    assert descriptor_matches(spec, X710_A)  # 0200xx
    assert not descriptor_matches(spec, _desc("0:0:0.0", "8086", "9999", "020100"))


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "zzzz:0000",
        "8086:157",  # too short
        "8086:15722",  # too long
        "8086-1572",  # wrong separator
        "8086:1572:",  # trailing junk
        "8086:1572 ",  # trailing space
        "class=",
        "class=xyz",
        "class=0",  # 1 hex
        "class=020",  # 3 hex
        "class=02000",  # 5 hex
        "8086",  # bare vendor
        "8086:GGGG",
        "8086:1572:0200",
    ],
)
def test_malformed_spec_is_configuration_error(bad: str) -> None:
    with pytest.raises(CategorizedError) as exc:
        parse_match_spec(bad)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize("upper", ["8086:157A", "CLASS=02", "class=0A", "8086:157a "])
def test_uppercase_hex_rejected(upper: str) -> None:
    # The wire form is lowercase (ADR-0068); we fail closed rather than silently normalize.
    with pytest.raises(CategorizedError) as exc:
        parse_match_spec(upper)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_lowercase_hex_letters_accepted() -> None:
    spec = parse_match_spec("10de:1eb8")
    assert descriptor_matches(spec, _desc("0:0:0.0", "10de", "1eb8", "030000"))


# --- single-spec resolution: matched / config / capacity -------------------------------


def test_resolve_spec_matched_returns_free_candidates() -> None:
    result = resolve_spec("8086:1572", FLEET, claims=[])
    assert result.outcome is MatchOutcome.MATCHED
    assert {d["bdf"] for d in result.candidates} == {X710_A["bdf"], X710_B["bdf"]}


def test_resolve_spec_no_descriptor_anywhere_is_config() -> None:
    result = resolve_spec("dead:beef", FLEET, claims=[])
    assert result.outcome is MatchOutcome.CONFIG
    assert result.candidates == []


def test_resolve_spec_all_matches_claimed_is_capacity() -> None:
    claims = [_claim(X710_A["bdf"]), _claim(X710_B["bdf"])]
    result = resolve_spec("8086:1572", FLEET, claims=claims)
    assert result.outcome is MatchOutcome.CAPACITY
    assert result.candidates == []


def test_resolve_spec_some_claimed_still_matched() -> None:
    result = resolve_spec("8086:1572", FLEET, claims=[_claim(X710_A["bdf"])])
    assert result.outcome is MatchOutcome.MATCHED
    assert {d["bdf"] for d in result.candidates} == {X710_B["bdf"]}


def test_resolve_spec_class_matched() -> None:
    result = resolve_spec("class=01", FLEET, claims=[])
    assert result.outcome is MatchOutcome.MATCHED
    assert {d["bdf"] for d in result.candidates} == {NVME["bdf"]}


def test_resolve_spec_malformed_raises_not_returns() -> None:
    with pytest.raises(CategorizedError) as exc:
        resolve_spec("class=xyz", FLEET, claims=[])
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- multiset resolution: distinct devices ---------------------------------------------


def test_resolve_multiset_distinct_devices() -> None:
    result = resolve_multiset(["8086:1572", "8086:1572"], FLEET, claims=[])
    assert result.outcome is MatchOutcome.MATCHED
    chosen = {d["bdf"] for d in result.devices}
    assert chosen == {X710_A["bdf"], X710_B["bdf"]}  # two DIFFERENT cards
    assert len(result.devices) == 2


def test_resolve_multiset_mixed_specs() -> None:
    result = resolve_multiset(["8086:1572", "class=01"], FLEET, claims=[])
    assert result.outcome is MatchOutcome.MATCHED
    assert {d["bdf"] for d in result.devices} == {X710_A["bdf"], NVME["bdf"]}


def test_resolve_multiset_out_of_distinct_cards_is_capacity() -> None:
    # Two X710 specs but one already claimed → only one free card for two specs.
    result = resolve_multiset(["8086:1572", "8086:1572"], FLEET, claims=[_claim(X710_A["bdf"])])
    assert result.outcome is MatchOutcome.CAPACITY


def test_resolve_multiset_missing_model_is_config() -> None:
    result = resolve_multiset(["8086:1572", "dead:beef"], FLEET, claims=[])
    assert result.outcome is MatchOutcome.CONFIG


def test_resolve_multiset_config_wins_over_capacity() -> None:
    # One spec is busy (capacity), another is absent (config). Config is the harder denial
    # (it can never be satisfied on this host), so it dominates the aggregate outcome.
    claims = [_claim(X710_A["bdf"]), _claim(X710_B["bdf"])]
    result = resolve_multiset(["8086:1572", "dead:beef"], FLEET, claims=claims)
    assert result.outcome is MatchOutcome.CONFIG


def test_resolve_multiset_empty_is_matched_no_devices() -> None:
    result = resolve_multiset([], FLEET, claims=[])
    assert result.outcome is MatchOutcome.MATCHED
    assert result.devices == []


def test_resolve_multiset_malformed_raises() -> None:
    with pytest.raises(CategorizedError) as exc:
        resolve_multiset(["8086:1572", "nope"], FLEET, claims=[])
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
