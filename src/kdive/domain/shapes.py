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
