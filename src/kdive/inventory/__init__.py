"""Typed model + loader for the ``systems.toml`` v2 inventory document (ADR-0112).

The inventory package parses an operator-authored ``systems.toml`` into a typed
:class:`~kdive.inventory.model.InventoryDoc` and surfaces every parse/validation
failure as a single :class:`~kdive.inventory.errors.InventoryError`, so callers
fault-isolate one exception type.
"""

from __future__ import annotations

from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory, load_inventory_optional
from kdive.inventory.model import (
    BuildHostInstance,
    BuildSource,
    FaultInjectInstance,
    ImageEntry,
    ImageSource,
    InventoryDoc,
    LocalLibvirtInstance,
    RemoteLibvirtInstance,
    S3Source,
    StagedSource,
)

__all__ = [
    "BuildHostInstance",
    "BuildSource",
    "FaultInjectInstance",
    "ImageEntry",
    "ImageSource",
    "InventoryDoc",
    "InventoryError",
    "LocalLibvirtInstance",
    "RemoteLibvirtInstance",
    "S3Source",
    "StagedSource",
    "load_inventory",
    "load_inventory_optional",
]
