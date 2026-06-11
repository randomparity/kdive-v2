"""Guest-contract validation for a rootfs image (ADR-0092, ADR-0093).

A bootable kdive rootfs must carry the provider's guest contract: a guest agent (for the
in-target exec channel), kdump (crash capture), drgn (live introspection), and the allowlisted
in-guest helpers. ``validate_guest_contract`` libguestfs-inspects the image and raises a
``CategorizedError(CONFIGURATION_ERROR)`` **naming the first missing element**, so a build or
upload that lacks the contract is rejected before it is registered — never published as bootable.

The slow libguestfs probe is an **injected seam** (``inspect``) defaulting to a real
``guestfish``-based existence check. Callers offload this synchronous, environment-bound call via
``asyncio.to_thread``. This is the milestone's single guest-contract validator: the IMAGE_BUILD
handler (#285) is its first consumer; the private-upload service (#286) reuses it.
"""

from __future__ import annotations

import subprocess  # noqa: S404 - guestfish invoked with fixed argv, no shell
from collections.abc import Callable, Sequence
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory

# Each contract element maps to a canonical in-guest path whose presence proves the element is
# installed. A guest agent, the kdump service unit, the drgn module, and the allowlisted-helper
# marker installed by the build plane.
GUEST_CONTRACT_PATHS: dict[str, str] = {
    "agent": "/usr/sbin/qemu-ga",
    "kdump": "/usr/lib/systemd/system/kdump.service",
    "drgn": "/usr/lib/kdive/drgn-ready",
    "helpers": "/usr/lib/kdive/allowlisted-helpers",
}

_GUESTFISH_TIMEOUT_S = 5 * 60

type InspectSeam = Callable[[Path, Sequence[str]], set[str]]


def _real_inspect(qcow2_path: Path, candidates: Sequence[str]) -> set[str]:
    """Return the subset of ``candidates`` that exist in ``qcow2_path`` (read-only guestfish).

    Adds each candidate as an ``-i``-inspected ``exists`` probe in a single guestfish invocation,
    parsing the ``true``/``false`` lines back to the present set.

    Raises:
        CategorizedError: guestfish is absent (``MISSING_DEPENDENCY``), times out, or fails
            (``INFRASTRUCTURE_FAILURE``).
    """
    commands = "\n".join(f"exists {path}" for path in candidates)
    argv = ["guestfish", "--ro", "-a", str(qcow2_path), "-i"]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted inputs
            argv,
            input=commands + "\n",
            capture_output=True,
            text=True,
            timeout=_GUESTFISH_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            "guestfish is not installed; cannot validate the image guest contract",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"tool": "guestfish"},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "guest-contract inspection exceeded its timeout",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": _GUESTFISH_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "guest-contract inspection failed",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": result.stderr[-2000:]},
        )
    verdicts = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return {path for path, verdict in zip(candidates, verdicts, strict=False) if verdict == "true"}


def validate_guest_contract(
    qcow2_path: Path,
    *,
    required: Sequence[str],
    inspect: InspectSeam = _real_inspect,
) -> None:
    """Confirm ``qcow2_path`` carries every element in ``required``; raise naming the first absent.

    Args:
        qcow2_path: The local path to the rootfs qcow2 to inspect.
        required: The guest-contract element tags the image must satisfy (a subset of
            :data:`GUEST_CONTRACT_PATHS` — e.g. ``agent``, ``kdump``, ``drgn``, ``helpers``).
        inspect: The libguestfs inspection seam; defaults to a real ``guestfish`` probe. Tests
            inject a stub so they need no libguestfs.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``required`` names an unknown element or the
            image is missing one (the message and ``details['missing']`` name it); the probe's
            ``MISSING_DEPENDENCY``/``INFRASTRUCTURE_FAILURE`` propagate.
    """
    unknown = [element for element in required if element not in GUEST_CONTRACT_PATHS]
    if unknown:
        raise CategorizedError(
            f"unknown guest-contract element {unknown[0]!r}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"missing": unknown[0], "known": sorted(GUEST_CONTRACT_PATHS)},
        )
    candidates = [GUEST_CONTRACT_PATHS[element] for element in required]
    present_paths = inspect(qcow2_path, candidates)
    for element in required:
        if GUEST_CONTRACT_PATHS[element] not in present_paths:
            raise CategorizedError(
                f"image is missing the required guest-contract element {element!r}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"missing": element, "path": GUEST_CONTRACT_PATHS[element]},
            )
