"""Admission-side PCIe resolve/claim helpers (ADR-0068).

The matcher (:mod:`kdive.domain.pcie`) is pure: it resolves a spec union against a list of
descriptors minus a list of active claims. This module supplies the two admission-time
inputs the matcher needs and wraps the resolution the in-lock claim performs:

- :func:`descriptors_for` reads + **validates** a host's static descriptor list out of
  ``capabilities`` (host-derived, untrusted — one malformed entry never blanks the pool).
- :func:`active_claims` derives the host's occupancy set from the ``pcie_claim`` of its
  **non-terminal** allocations, using the shared :data:`NON_TERMINAL_STATES` set (which
  admission reuses) so the occupancy predicate can never drift from the capacity predicate.
- :func:`resolve_union` resolves the requested spec union to **distinct** free devices,
  returning the matcher's config-vs-capacity outcome (never raising on busy/absent — only
  malformed grammar raises, fail-closed).

The resolution must run **inside the per-Resource lock** (ADR-0068 Consequences): a locked
read-modify-write, never pre-lock validation, so two requests cannot both resolve the last
free device before either claims it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg import AsyncConnection

from kdive.domain.pcie import (
    PCIE_DEVICES_KEY,
    MultisetResolution,
    PCIeClaim,
    PCIeDescriptor,
    resolve_multiset,
)
from kdive.domain.state import AllocationState

if TYPE_CHECKING:
    from uuid import UUID

    from kdive.domain.models import Resource

# Allocation states that hold a live PCIe claim — the occupancy set, identical to
# admission's capacity predicate (REQUESTED/GRANTED/ACTIVE/RELEASING). Derived from the
# enum, not literal strings, so it cannot drift if the state machine gains a value. A
# device a non-terminal allocation holds is never double-booked; a terminal allocation's
# snapshot stops counting.
NON_TERMINAL_STATES = (
    AllocationState.REQUESTED,
    AllocationState.GRANTED,
    AllocationState.ACTIVE,
    AllocationState.RELEASING,
)
NON_TERMINAL_STATES_VALUES = tuple(state.value for state in NON_TERMINAL_STATES)

_DESCRIPTOR_FIELDS = ("bdf", "vendor_id", "device_id", "class_code", "label")


def descriptors_for(resource: Resource) -> list[PCIeDescriptor]:
    """Read + validate the host's static PCIe descriptors from ``capabilities``.

    The descriptor list is host-derived (discovery wrote it from libvirt/lspci) and so
    crosses a trust boundary: a non-list value, a non-dict entry, or an entry missing a
    required string field is dropped rather than trusted, so one malformed device never
    blanks the inventory or feeds a non-string into the matcher. Returns the well-formed
    descriptors in their advertised order.
    """
    raw = resource.capabilities.get(PCIE_DEVICES_KEY)
    if not isinstance(raw, list):
        return []
    descriptors: list[PCIeDescriptor] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if any(not isinstance(entry.get(field), str) for field in _DESCRIPTOR_FIELDS):
            continue
        descriptors.append(
            PCIeDescriptor(
                bdf=entry["bdf"],
                vendor_id=entry["vendor_id"],
                device_id=entry["device_id"],
                class_code=entry["class_code"],
                label=entry["label"],
            )
        )
    return descriptors


async def active_claims(conn: AsyncConnection, resource_id: UUID) -> list[PCIeClaim]:
    """Return the host's occupancy set: every device held by a non-terminal allocation.

    Unions the ``pcie_claim`` snapshots of the host's allocations in a non-terminal state
    (the same set admission's capacity check counts), so a ``releasing`` allocation's
    device still reads as occupied and a ``released``/``expired``/``failed`` one's does
    not. The caller resolving + claiming a device must run this under the per-Resource
    lock for the read to be authoritative.
    """
    claims: list[PCIeClaim] = []
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pcie_claim FROM allocations "
            "WHERE resource_id = %s AND state = ANY(%s) AND pcie_claim <> '[]'::jsonb",
            (resource_id, list(NON_TERMINAL_STATES_VALUES)),
        )
        rows = await cur.fetchall()
    for row in rows:
        for held in row[0]:
            claims.append(
                PCIeClaim(bdf=held["bdf"], vendor_id=held["vendor_id"], device_id=held["device_id"])
            )
    return claims


def resolve_union(
    specs: list[str], descriptors: list[PCIeDescriptor], *, claims: list[PCIeClaim]
) -> MultisetResolution:
    """Resolve the requested spec union to distinct free devices (the matcher's multiset).

    ``specs`` is the already-composed union of explicit ``pcie_devices`` plus any resolved
    shape ``pcie_match``. An empty union resolves to ``MATCHED`` with no devices — a
    non-PCIe request claims nothing. Returns the matcher's ``MATCHED`` / ``CONFIG`` /
    ``CAPACITY`` outcome; only malformed grammar raises.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if any spec is malformed grammar.
    """
    return resolve_multiset(specs, descriptors, claims=claims)
