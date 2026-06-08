"""The system-shape resolver: name → sizing tuple, fail-closed (ADR-0067).

A shape names a curated sizing preset (``small`` … ``max``, seeded by migration 0013).
Resolving a shape yields one :class:`ShapeSizing` tuple ``{vcpus, memory_mb, disk_gb,
pcie_match?}`` — the size admission prices/capacity-checks and provisioning boots, so the
two can never disagree. The mapping is exact: ``memory_mb`` is a whole-GB multiple (the
:class:`~kdive.domain.models.SystemShape` model and the migration CHECK both enforce it),
so the cost Selector's ``memory_mb → memory_gb`` is lossless.

A shape fixes **size only**: ``cost_class`` (and therefore price) is resolved admission-side
from the chosen Resource, never from the shape, so the same shape on a costlier host costs
more. Resolution **fails closed** — an unknown name is a ``configuration_error``, never a
silent default (mirrors :func:`kdive.domain.cost.resolve_coeff`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from kdive.db.repositories import SYSTEM_SHAPES
from kdive.domain.errors import CategorizedError, ErrorCategory

if TYPE_CHECKING:
    from psycopg import AsyncConnection


class ShapeSizing(BaseModel):
    """The resolved sizing a shape fixes (ADR-0067).

    Carries ``vcpus`` / ``memory_mb`` / ``disk_gb`` and the optional ``pcie_match``;
    **not** ``cost_class``, which stays host-resolved at admission. ``memory_mb`` is a
    whole-GB multiple by the shape's own constraint, so a caller may map it to ``memory_gb``
    by integer division without loss.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vcpus: int
    memory_mb: int
    disk_gb: int
    pcie_match: str | None = None


async def resolve_shape(conn: AsyncConnection, name: str) -> ShapeSizing:
    """Resolve a shape ``name`` to its sizing tuple from ``system_shapes``.

    Fails closed: a name with no catalog row is a ``configuration_error``, never a silent
    default (ADR-0067). Reads the persisted catalog, never request data.

    Args:
        conn: An async connection to the migrated database.
        name: The shape name to resolve (e.g. ``"medium"``).

    Returns:
        The resolved :class:`ShapeSizing` for ``name``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``name`` has no catalog row.
    """
    shape = await SYSTEM_SHAPES.get(conn, name)
    if shape is None:
        raise CategorizedError(
            f"system shape {name!r} is not in the catalog",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"shape": name},
        )
    return ShapeSizing(
        vcpus=shape.vcpus,
        memory_mb=shape.memory_mb,
        disk_gb=shape.disk_gb,
        pcie_match=shape.pcie_match,
    )


# A shape's memory_mb maps to the cost Selector's memory_gb exactly (the whole-GB constraint).
_MB_PER_GB = 1024


class ResolvedSizing(BaseModel):
    """The unified allocation sizing after a shape-XOR-custom request is resolved (ADR-0067).

    ``vcpus`` / ``memory_gb`` are the priced size the cost Selector models; ``disk_gb`` and
    ``pcie_match`` are carried onward (to provisioning and PCIe admission), not priced;
    ``shape`` is the named preset the size came from (``None`` for full-custom), recorded as
    a label. This is the single authority for pricing, the capacity check, and the booted
    domain — admitted size and booted size are one number by construction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vcpus: int
    memory_gb: int
    disk_gb: int
    pcie_match: str | None = None
    shape: str | None = None


async def resolve_request_sizing(
    conn: AsyncConnection,
    *,
    shape: str | None,
    vcpus: int | None,
    memory_gb: int | None,
    disk_gb: int | None,
) -> ResolvedSizing:
    """Resolve a shape-XOR-custom request to one :class:`ResolvedSizing` (ADR-0067).

    A named ``shape`` resolves through :func:`resolve_shape` (fail-closed on an unknown
    name) and maps ``memory_mb → memory_gb`` losslessly. A full-custom triple is taken as
    given. The shape-XOR-custom rule is enforced at the request-payload boundary, so this
    fails closed if it ever sees an incomplete custom triple (defence in depth).

    Args:
        conn: An async connection to the migrated database.
        shape: The named shape, or ``None`` for a full-custom request.
        vcpus: Custom vCPU count (required when ``shape`` is ``None``).
        memory_gb: Custom memory in GB (required when ``shape`` is ``None``).
        disk_gb: Custom disk in GB (required when ``shape`` is ``None``).

    Returns:
        The unified :class:`ResolvedSizing`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an unknown shape or an incomplete
            custom triple.
    """
    if shape is not None:
        sizing = await resolve_shape(conn, shape)
        return ResolvedSizing(
            vcpus=sizing.vcpus,
            memory_gb=sizing.memory_mb // _MB_PER_GB,
            disk_gb=sizing.disk_gb,
            pcie_match=sizing.pcie_match,
            shape=shape,
        )
    if vcpus is None or memory_gb is None or disk_gb is None:
        raise CategorizedError(
            "a full-custom request must supply vcpus, memory_gb, and disk_gb",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return ResolvedSizing(vcpus=vcpus, memory_gb=memory_gb, disk_gb=disk_gb)
