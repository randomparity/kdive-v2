"""The reusable PCIe selection model and match-spec matcher (ADR-0068).

PCIe is a **selection axis**: a host advertises a list of static descriptors
(:data:`PCIE_DEVICES_KEY` on ``capabilities``) and a request references a device by a
portable **match spec** — never a host-local BDF. This module owns the spec grammar and the
matcher that three M1.4 surfaces reuse (admission resolution, fleet availability, systems
filter). Occupancy is **derived**: the matcher subtracts the BDFs held by active claims; it
never reads or writes a ``free`` flag (a re-scan must not un-claim a booked device).

The matcher distinguishes the two PCIe denial modes in its **return value**, never by
raising: ``CONFIG`` (no host descriptor matches the spec — the card is not in the fleet) vs.
``CAPACITY`` (the card exists but every match is currently claimed — busy, queueable).
Malformed grammar is the one raising path: a :class:`CategorizedError` with
:attr:`ErrorCategory.CONFIGURATION_ERROR`, so a bad spec is a structured failure, never an
uncaught exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TypedDict

from kdive.domain.errors import CategorizedError, ErrorCategory

PCIE_DEVICES_KEY = "pcie_devices"
"""The Resource ``capabilities`` key carrying the static PCIe descriptor list."""


class PCIeDescriptor(TypedDict):
    """A static, host-local PCIe device descriptor written by discovery (ADR-0068).

    No ``free`` flag: occupancy is derived from active claims, never stored here.
    """

    bdf: str
    vendor_id: str
    device_id: str
    class_code: str
    label: str


class PCIeClaim(TypedDict):
    """A snapshot of a device held by an active (non-terminal) allocation.

    The matcher subtracts claims **by ``bdf``** — a claim names a resolved host-local device.
    """

    bdf: str
    vendor_id: str
    device_id: str


class MatchOutcome(StrEnum):
    """The resolution outcome for a spec or a multiset.

    ``CONFIG`` and ``CAPACITY`` are the two ADR-0068 denial modes; they are returned, never
    raised, so admission can queue a ``CAPACITY`` denial and hard-deny a ``CONFIG`` one.
    """

    MATCHED = auto()
    CONFIG = auto()
    CAPACITY = auto()


class _Kind(StrEnum):
    VENDOR_DEVICE = auto()
    CLASS = auto()


@dataclass(frozen=True, slots=True)
class MatchSpec:
    """A parsed, validated match spec.

    ``vendor_id``/``device_id`` are set for a ``vendor:device`` spec; ``class_prefix`` (a 2-
    or 4-hex string) for a ``class=`` spec. Build only via :func:`parse_match_spec`.
    """

    kind: _Kind
    vendor_id: str | None = None
    device_id: str | None = None
    class_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class SpecResolution:
    """The result of resolving one spec: an outcome plus the free matching candidates."""

    outcome: MatchOutcome
    candidates: list[PCIeDescriptor]


@dataclass(frozen=True, slots=True)
class MultisetResolution:
    """The result of resolving a multiset: an outcome plus the distinct devices chosen."""

    outcome: MatchOutcome
    devices: list[PCIeDescriptor]


_VENDOR_DEVICE_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")
_CLASS_RE = re.compile(r"^class=([0-9a-f]{2}|[0-9a-f]{4})$")


def parse_match_spec(spec: str) -> MatchSpec:
    """Parse and validate a match spec into a :class:`MatchSpec`.

    Grammar (lowercase hex, exact — uppercase is rejected, not normalized, so the wire form
    stays canonical):

    - ``vendor:device`` — ``4hex:4hex`` (e.g. ``8086:1572``), an exact device-model match.
    - ``class=NN`` — a ``2hex`` class high byte (e.g. ``class=02`` = any network controller).
    - ``class=NNNN`` — a ``4hex`` class+subclass exact match (e.g. ``class=0200``).

    Args:
        spec: The raw match-spec string.

    Returns:
        The parsed spec.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``spec`` does not match the grammar.
    """
    if _VENDOR_DEVICE_RE.match(spec):
        vendor_id, device_id = spec.split(":")
        return MatchSpec(kind=_Kind.VENDOR_DEVICE, vendor_id=vendor_id, device_id=device_id)
    class_match = _CLASS_RE.match(spec)
    if class_match:
        return MatchSpec(kind=_Kind.CLASS, class_prefix=class_match.group(1))
    raise CategorizedError(
        f"malformed PCIe match spec {spec!r}: expected '<4hex>:<4hex>' or 'class=<2|4 hex>'",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"spec": spec},
    )


def descriptor_matches(spec: MatchSpec, descriptor: PCIeDescriptor) -> bool:
    """Return whether ``descriptor`` satisfies ``spec`` (occupancy not considered)."""
    if spec.kind is _Kind.VENDOR_DEVICE:
        return (
            descriptor["vendor_id"] == spec.vendor_id and descriptor["device_id"] == spec.device_id
        )
    prefix = spec.class_prefix or ""
    return descriptor["class_code"].startswith(prefix)


def resolve_spec(
    spec: str, descriptors: list[PCIeDescriptor], *, claims: list[PCIeClaim]
) -> SpecResolution:
    """Resolve one match spec against ``descriptors`` minus the devices held by ``claims``.

    Args:
        spec: A raw match spec (validated here; malformed input raises).
        descriptors: The host's static PCIe descriptors.
        claims: Active claims; their ``bdf``\\ s are subtracted from the candidate pool.

    Returns:
        ``MATCHED`` with the free candidates, ``CAPACITY`` if every match is claimed, or
        ``CONFIG`` if no descriptor matches the spec at all.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``spec`` is malformed.
    """
    parsed = parse_match_spec(spec)
    matching = [d for d in descriptors if descriptor_matches(parsed, d)]
    if not matching:
        return SpecResolution(MatchOutcome.CONFIG, [])
    claimed = {c["bdf"] for c in claims}
    free = [d for d in matching if d["bdf"] not in claimed]
    if not free:
        return SpecResolution(MatchOutcome.CAPACITY, [])
    return SpecResolution(MatchOutcome.MATCHED, free)


def resolve_multiset(
    specs: list[str], descriptors: list[PCIeDescriptor], *, claims: list[PCIeClaim]
) -> MultisetResolution:
    """Resolve a multiset of specs to **distinct** free devices (one per spec).

    Two identical specs resolve to two different devices; a device is consumed by at most one
    spec. The aggregate outcome follows the worst per-spec result, with ``CONFIG`` dominating
    ``CAPACITY``: a spec no descriptor matches can never be satisfied on this host (a hard
    denial), whereas a busy spec is queueable, so a multiset mixing both is reported
    ``CONFIG``.

    Args:
        specs: Raw match specs (each validated; a malformed one raises).
        descriptors: The host's static PCIe descriptors.
        claims: Active claims subtracted before assignment.

    Returns:
        ``MATCHED`` with the chosen distinct devices, else ``CONFIG`` / ``CAPACITY``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if any spec is malformed.
    """
    parsed_specs = [parse_match_spec(s) for s in specs]
    claimed = {c["bdf"] for c in claims}
    available = [d for d in descriptors if d["bdf"] not in claimed]
    taken_bdfs: set[str] = set()
    chosen: list[PCIeDescriptor] = []
    saw_config = False
    saw_capacity = False
    for parsed in parsed_specs:
        pick = _pick_distinct(parsed, available, taken_bdfs)
        if pick is not None:
            taken_bdfs.add(pick["bdf"])
            chosen.append(pick)
            continue
        if any(descriptor_matches(parsed, d) for d in descriptors):
            saw_capacity = True
        else:
            saw_config = True
    if saw_config:
        return MultisetResolution(MatchOutcome.CONFIG, [])
    if saw_capacity:
        return MultisetResolution(MatchOutcome.CAPACITY, [])
    return MultisetResolution(MatchOutcome.MATCHED, chosen)


def _pick_distinct(
    spec: MatchSpec, available: list[PCIeDescriptor], taken: set[str]
) -> PCIeDescriptor | None:
    """Return the first free, not-yet-assigned descriptor matching ``spec``, or ``None``."""
    for descriptor in available:
        if descriptor["bdf"] not in taken and descriptor_matches(spec, descriptor):
            return descriptor
    return None
