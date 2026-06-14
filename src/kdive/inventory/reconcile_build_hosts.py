"""The build-host merge-reconcile (M2.6 #394, ADR-0112).

:func:`reconcile_build_hosts` applies the ``systems.toml`` ``[[build_host]]`` declarations
onto the ``build_hosts`` table (``0027`` / ``0029``). ``managed_by`` governs existence; a
config-declared host is created/updated as ``managed_by='config'`` and carries
``base_image_volume`` / ``workspace_root`` / ``max_concurrent``.

Identity is the build-host **``name``** (already ``text UNIQUE NOT NULL`` in ``0027``), so the
upsert keys on it directly — no migration change. Adopt-on-collision: a config-declared host
whose ``name`` matches an existing ``managed_by='runtime'`` row is **adopted** (flipped to
``config``), never duplicated; the seeded ``worker-local`` baseline from ``0027`` is
``runtime``, so declaring it in config adopts it. (A runtime ``build_hosts.register`` of a name
that already exists is rejected by the ``name`` UNIQUE constraint, regardless of ownership.)

Kind coverage: the v2 ``[[build_host]]`` model carries no ``address`` / ``ssh_credential_ref``,
so only ``local`` and ``ephemeral_libvirt`` hosts (which need neither) are fully expressible in
config. A config-declared ``ssh`` host cannot satisfy the ``build_hosts_fields_check`` CHECK, so
it is **warned and skipped** rather than aborting the pass — ssh hosts are registered
imperatively via ``build_hosts.register``, which carries those fields.

Prune is DB-guarded: ``build_host_leases`` FKs ``build_hosts(id) ON DELETE RESTRICT``, so a host
with an in-flight build lease cannot be deleted. Prune therefore **cordons** a busy host
(``enabled = false`` — the disable mechanism the scheduler and reachability probe both honor)
and only **deletes** an idle config host's row, matching the reaper-style refuse-if-live
contract. Prune touches only ``managed_by='config'`` rows. The cordon path SELECTs the
``build_hosts`` row ``FOR UPDATE`` before checking the lease, which conflicts with the implicit
``FOR KEY SHARE`` a concurrent ``build_host_leases`` INSERT takes on the parent row (the FK
check). The two therefore serialize: a lease can never land between the liveness check and the
delete to make the delete hit ``ON DELETE RESTRICT`` and abort the pass.

Declarative ownership note: re-declaring (or adopting) a config host always resets
``enabled = true``, so config is the source of truth for a config-owned host's schedulability.
A consequence is that ``build_hosts.disable`` on a config-owned host is reverted on the next
reconcile pass — to take a config host out of rotation, remove it from ``systems.toml`` (an
idle host's row is then pruned; a busy one is cordoned until its lease drains).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.models import ManagedBy
from kdive.inventory.model import BuildHostInstance, InventoryDoc
from kdive.inventory.reconcile import (
    ReconcileDiff,
    ReconcileRecord,
    inventory_pass_lock,
    prune_or_cordon_build_host,
)

_log = logging.getLogger(__name__)

_CONFIG = ManagedBy.CONFIG.value

# Kinds the v2 [[build_host]] model can fully express (it carries no address/ssh_credential_ref,
# which the build_hosts_fields_check CHECK requires for the 'ssh' kind).
_CONFIG_EXPRESSIBLE_KINDS = ("local", "ephemeral_libvirt")


async def reconcile_build_hosts(conn: AsyncConnection, doc: InventoryDoc) -> ReconcileDiff:
    """Apply ``doc``'s ``[[build_host]]`` declarations onto ``build_hosts``; prune departed.

    Held under the same session-scoped inventory lock as the image/resource passes, so the
    multi-transaction pass (batched upsert + per-row prunes) never races a second pass into the
    ``name`` UNIQUE constraint.

    Args:
        conn: The reconcile pass connection (a fresh transaction is opened per phase).
        doc: The parsed inventory document.

    Returns:
        The :class:`ReconcileDiff` for the build-host pass.
    """
    diff = ReconcileDiff()
    async with inventory_pass_lock(conn):
        await _upsert_config_build_hosts(conn, doc, diff)
        await _prune_departed(conn, doc, diff)
    return diff


async def _upsert_config_build_hosts(
    conn: AsyncConnection, doc: InventoryDoc, diff: ReconcileDiff
) -> None:
    """Create or change-detectingly update each config-expressible ``[[build_host]]`` by name."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        for inst in doc.build_host:
            reason = _unexpressible_reason(inst)
            if reason is not None:
                diff.warned.append(_record(inst.name, reason))
                _log.warning(
                    "inventory: build host %r not config-expressible: %s", inst.name, reason
                )
                continue
            await _upsert_one(cur, inst, diff)


def _unexpressible_reason(inst: BuildHostInstance) -> str | None:
    """Return why a config build host cannot be realized, or ``None`` if it can.

    The v2 model lacks ``address`` / ``ssh_credential_ref``, so an ``ssh`` host can never
    satisfy the field CHECK; a ``local`` host must carry no ``base_image_volume`` and an
    ``ephemeral_libvirt`` host must carry one (the ``build_hosts_fields_check`` CHECK). Catching
    these here keeps an invalid declaration from aborting the whole pass on a CHECK violation.
    """
    if inst.kind not in _CONFIG_EXPRESSIBLE_KINDS:
        return (
            f"kind {inst.kind!r} is not config-expressible "
            f"(only {', '.join(_CONFIG_EXPRESSIBLE_KINDS)}); register it imperatively"
        )
    if inst.kind == "ephemeral_libvirt" and not (
        inst.base_image_volume and inst.base_image_volume.strip()
    ):
        return "an ephemeral_libvirt build host requires a base_image_volume"
    if inst.kind == "local" and inst.base_image_volume:
        return "base_image_volume is not valid for a local build host"
    return None


async def _upsert_one(cur: Any, inst: BuildHostInstance, diff: ReconcileDiff) -> None:
    """Create or update one config build-host row keyed by ``name`` (adopt-on-collision).

    A row whose ``name`` already exists is updated in place and flipped to ``managed_by='config'``
    (adopting a ``runtime`` row), and ``enabled`` is reset ``true`` so re-declaring a previously
    cordoned host re-enables it. Only the config-owned fields are written; ``state`` is left to
    the reachability probe. Append to ``created``/``updated`` only on a real change so a steady
    state is a no-op (the idempotency contract).
    """
    await cur.execute(
        "SELECT id, kind, base_image_volume, workspace_root, max_concurrent, enabled, managed_by "
        "FROM build_hosts WHERE name = %s FOR UPDATE",
        (inst.name,),
    )
    row = await cur.fetchone()
    base_image_volume = inst.base_image_volume if inst.kind == "ephemeral_libvirt" else None
    if row is None:
        await cur.execute(
            "INSERT INTO build_hosts "
            "(name, kind, base_image_volume, workspace_root, max_concurrent, enabled, managed_by) "
            "VALUES (%s, %s, %s, %s, %s, true, %s)",
            (
                inst.name,
                inst.kind,
                base_image_volume,
                inst.workspace_root,
                inst.max_concurrent,
                _CONFIG,
            ),
        )
        diff.created.append(_record(inst.name))
        return
    changed = (
        row["kind"] != inst.kind
        or row["base_image_volume"] != base_image_volume
        or row["workspace_root"] != inst.workspace_root
        or row["max_concurrent"] != inst.max_concurrent
        or row["enabled"] is not True
        or str(row["managed_by"]) != _CONFIG
    )
    if changed:
        await cur.execute(
            "UPDATE build_hosts SET kind = %s, base_image_volume = %s, workspace_root = %s, "
            "max_concurrent = %s, enabled = true, managed_by = %s WHERE id = %s",
            (
                inst.kind,
                base_image_volume,
                inst.workspace_root,
                inst.max_concurrent,
                _CONFIG,
                row["id"],
            ),
        )
        diff.updated.append(_record(inst.name))


async def _prune_departed(conn: AsyncConnection, doc: InventoryDoc, diff: ReconcileDiff) -> None:
    """Prune (or cordon) each config build host whose ``name`` left the file."""
    declared = {inst.name for inst in doc.build_host if _unexpressible_reason(inst) is None}
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT id, name FROM build_hosts WHERE managed_by = %s", (_CONFIG,))
        rows = await cur.fetchall()
    for row in rows:
        name = str(row["name"])
        if name in declared:
            continue
        outcome = await prune_or_cordon_build_host(conn, _row_id(row), name)
        record = ReconcileRecord(name=name, entry=f"build_host[{name}]")
        if outcome.cordoned:
            diff.cordoned.append(record)
            _log.info(
                "inventory: config build host %s still has a lease; cordoned (disabled)", name
            )
        elif outcome.pruned:
            diff.pruned.append(record)
            _log.info("inventory: config build host %s absent from config; row pruned", name)


def _record(name: str, detail: str = "") -> ReconcileRecord:
    return ReconcileRecord(name=name, entry=f"build_host[{name}]", detail=detail)


def _row_id(row: dict[str, Any]) -> UUID:
    value = row["id"]
    assert isinstance(value, UUID)
    return value


__all__ = ["reconcile_build_hosts"]
