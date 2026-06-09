"""The reusable PCIe selection model and match-spec matcher (ADR-0068).

PCIe is a **selection axis**: a host advertises a list of static descriptors
(:data:`PCIE_DEVICES_KEY` on ``capabilities``) and a request references a device by a
portable **match spec** — never a host-local BDF. This module owns the spec grammar and the
matcher shared by admission resolution, fleet availability, and systems filters. Occupancy is
**derived**: the matcher subtracts the BDFs held by active claims; it
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

    Field contract (the matcher assumes discovery-normalized values; consumers that build
    descriptors from other sources — DB rows, future providers — must match it):

    - ``vendor_id`` / ``device_id``: bare lowercase **4-hex** (no ``0x``).
    - ``class_code``: bare lowercase **6-hex** (class+subclass+prog-if, e.g. ``020000``); the
      ``class=`` matcher prefix-slices it, so a short/empty value silently under-matches.
    - ``bdf``: canonical ``DDDD:BB:SS.F`` lowercase hex.
    - ``label``: opaque display string (untrusted libvirt/lspci text; never an identity).
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
    spec. Assignment is an exact **maximum bipartite matching** (Kuhn's augmenting paths), not
    a first-fit greedy pass, so overlapping specs (a broad ``class=`` and a narrow
    ``vendor:device`` that share a card) never spuriously fail when a valid distinct
    assignment exists — the outcome is order-independent.

    The aggregate outcome, when a full assignment is impossible, follows ADR-0068's denial
    split with ``CONFIG`` dominating ``CAPACITY``: a spec that **no descriptor matches at all**
    can never be satisfied on this host (a hard config denial), whereas a spec whose matches
    exist but are exhausted (claimed, or out of distinct free cards) is a queueable capacity
    denial. A multiset mixing the two is reported ``CONFIG``.

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
    if any(not any(descriptor_matches(p, d) for d in descriptors) for p in parsed_specs):
        return MultisetResolution(MatchOutcome.CONFIG, [])
    claimed = {c["bdf"] for c in claims}
    available = [d for d in descriptors if d["bdf"] not in claimed]
    assignment = _max_bipartite_match(parsed_specs, available)
    if assignment is None:
        return MultisetResolution(MatchOutcome.CAPACITY, [])
    return MultisetResolution(MatchOutcome.MATCHED, [available[i] for i in assignment])


@dataclass(slots=True)
class _Matching:
    """Mutable bipartite-matching state for :func:`_max_bipartite_match`."""

    specs: list[MatchSpec]
    available: list[PCIeDescriptor]
    spec_to_device: dict[int, int]  # spec index -> chosen available index
    device_to_spec: dict[int, int]  # available index -> owning spec index


def _max_bipartite_match(
    specs: list[MatchSpec], available: list[PCIeDescriptor]
) -> list[int] | None:
    """Assign each spec a distinct ``available`` index via Kuhn's algorithm.

    Returns the per-spec descriptor index list when every spec is matched, else ``None``
    (no perfect matching exists — fewer distinct free devices than specs).
    """
    state = _Matching(specs=specs, available=available, spec_to_device={}, device_to_spec={})
    for spec_idx in range(len(specs)):
        if not _augment(state, spec_idx, set()):
            return None
    return [state.spec_to_device[i] for i in range(len(specs))]


def _augment(state: _Matching, spec_idx: int, visited: set[int]) -> bool:
    """Match ``spec_idx`` to a device, displacing a prior owner along an augmenting path."""
    spec = state.specs[spec_idx]
    for device_idx, descriptor in enumerate(state.available):
        if device_idx in visited or not descriptor_matches(spec, descriptor):
            continue
        visited.add(device_idx)
        owner = state.device_to_spec.get(device_idx)
        if owner is None or _augment(state, owner, visited):
            state.device_to_spec[device_idx] = spec_idx
            state.spec_to_device[spec_idx] = device_idx
            return True
    return False
