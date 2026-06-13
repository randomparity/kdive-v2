"""The shared chunk-key helper (ADR-0104 §1)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError
from kdive.provider_components.artifacts import chunk_key, owner_prefix

_PREFIX = owner_prefix("local", "runs", "11111111-1111-1111-1111-111111111111")


def test_chunk_key_is_zero_padded_one_based() -> None:
    assert chunk_key(_PREFIX, "vmlinux", 1) == f"{_PREFIX}vmlinux.part0001"
    assert chunk_key(_PREFIX, "vmlinux", 42) == f"{_PREFIX}vmlinux.part0042"


def test_chunk_key_rejects_non_positive_part_number() -> None:
    with pytest.raises(CategorizedError):
        chunk_key(_PREFIX, "vmlinux", 0)
