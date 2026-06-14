"""The ``kdive reconcile-systems`` operator command (M2.6 #391, ADR-0112).

Runs one inventory reconcile pass against the catalog, prints the resulting
:class:`~kdive.inventory.reconcile.ReconcileDiff`, and exits non-zero on an
:class:`~kdive.inventory.InventoryError`. Unlike the ``kdivectl`` MCP-client verbs, this is a
server-side command: it opens its own Postgres pool and object store and calls
:func:`~kdive.inventory.reconcile_images.reconcile_images` directly, the same engine the
reconciler loop triggers.

Path resolution mirrors the loop only for the default:

* an explicit ``--path`` to a missing file is an operator error (``load_inventory`` raises
  ``InventoryError``) — the operator named a path that is not there;
* the default ``KDIVE_SYSTEMS_TOML`` path, when absent, is a quiet no-op (exit 0):
  ``systems.toml`` is gitignored and an absent default is the normal pre-config state. It must
  not feed an empty document to ``reconcile_images``, which would prune every config row.
"""

from __future__ import annotations

import sys
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from kdive.config.core_settings import SYSTEMS_TOML
from kdive.inventory import InventoryError, load_inventory, load_inventory_optional
from kdive.inventory.model import InventoryDoc
from kdive.inventory.reconcile import ReconcileDiff
from kdive.inventory.reconcile_images import ImageHeadStore, reconcile_images

_EXIT_OK = 0
_EXIT_INVENTORY_ERROR = 1


async def reconcile_systems(
    path: Path | None, *, pool: AsyncConnectionPool, store: ImageHeadStore
) -> int:
    """Run one reconcile pass against ``pool``, print the diff, return the exit code.

    Args:
        path: An explicit ``systems.toml`` path, or ``None`` to use the default
            ``KDIVE_SYSTEMS_TOML`` path (an absent default is a quiet no-op).
        pool: The Postgres pool; the pass owns a fresh pooled connection for its duration.
        store: The object store, used only to HEAD ``s3`` objects for existence.

    Returns:
        ``0`` on success (including an absent default file), non-zero on an
        :class:`~kdive.inventory.InventoryError`.
    """
    try:
        doc = _load_doc(path)
    except InventoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_INVENTORY_ERROR
    if doc is None:
        print("no systems.toml present; nothing to reconcile")
        return _EXIT_OK
    async with pool.connection() as conn:
        diff = await reconcile_images(conn, doc, store)
    _print_diff(diff)
    return _EXIT_OK


def _load_doc(path: Path | None) -> InventoryDoc | None:
    """Load the inventory doc, raising on a missing explicit ``--path``.

    An explicit path is loaded with :func:`load_inventory` (a missing file is an error). The
    default path uses :func:`load_inventory_optional` (a missing file returns ``None``).
    """
    if path is not None:
        return load_inventory(path)
    import kdive.config as config

    # SYSTEMS_TOML carries a default, so get() is non-None outside a misconfigured registry.
    resolved = config.get(SYSTEMS_TOML) or "./systems.toml"
    return load_inventory_optional(Path(resolved))


def _print_diff(diff: ReconcileDiff) -> None:
    """Print the per-category reconcile diff, one entry per line under each header."""
    sections = (
        ("created", diff.created),
        ("updated", diff.updated),
        ("pruned", diff.pruned),
        ("cordoned", diff.cordoned),
        ("warned", diff.warned),
    )
    for label, records in sections:
        print(f"{label}: {len(records)}")
        for record in records:
            suffix = f" — {record.detail}" if record.detail else ""
            print(f"  {record.entry}{suffix}")
