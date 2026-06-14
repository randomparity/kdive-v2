"""Typed model + loader for the ``systems.toml`` v2 inventory document (ADR-0112).

The inventory package parses an operator-authored ``systems.toml`` into a typed
:class:`~kdive.inventory.model.InventoryDoc` and surfaces every parse/validation
failure as a single :class:`~kdive.inventory.errors.InventoryError`, so callers
fault-isolate one exception type.
"""
