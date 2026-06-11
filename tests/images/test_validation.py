"""Guest-contract validation of a built/uploaded rootfs image (ADR-0092/0093, issue #285).

``validate_guest_contract`` libguestfs-inspects the image and raises a
``CategorizedError(CONFIGURATION_ERROR)`` naming the first missing contract element. The slow
libguestfs probe is an injected seam so these tests run without libguestfs.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.validation import GUEST_CONTRACT_PATHS, InspectSeam, validate_guest_contract


def _present(*paths: str) -> InspectSeam:
    """An inspection seam reporting exactly ``paths`` as present in the image."""

    def _probe(qcow2_path: Path, candidates: Sequence[str]) -> set[str]:
        return {p for p in candidates if p in paths}

    return _probe


def test_passes_when_every_required_element_is_present(tmp_path: Path) -> None:
    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")
    required = ["agent", "kdump", "drgn"]
    present = _present(*[GUEST_CONTRACT_PATHS[r] for r in required])
    # Does not raise.
    validate_guest_contract(image, required=required, inspect=present)


def test_names_the_missing_element(tmp_path: Path) -> None:
    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")
    # kdump is present; the guest agent and drgn are absent.
    present = _present(GUEST_CONTRACT_PATHS["kdump"])

    with pytest.raises(CategorizedError) as err:
        validate_guest_contract(image, required=["agent", "kdump", "drgn"], inspect=present)

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
    # The error names the missing element (the first one), not a generic failure.
    assert "agent" in str(err.value)
    assert err.value.details.get("missing") == "agent"


def test_unknown_required_element_is_a_configuration_error(tmp_path: Path) -> None:
    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")

    with pytest.raises(CategorizedError) as err:
        validate_guest_contract(image, required=["nonsense"], inspect=_present())

    assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "nonsense" in str(err.value)


def test_empty_required_is_a_no_op(tmp_path: Path) -> None:
    image = tmp_path / "img.qcow2"
    image.write_bytes(b"")
    validate_guest_contract(image, required=[], inspect=_present())
