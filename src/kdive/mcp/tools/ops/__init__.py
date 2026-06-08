"""Platform control-plane and break-glass MCP tools (`ops.*`, ADR-0062).

The `ops.*` namespace holds platform-operator control-plane actions (reconcile, queue
control, capacity/cost tuning) and platform-admin break-glass. Each tool gates on the M1.1
``require_platform_role`` seam and audits cross-tenant actions. Distinct from
``kdive.mcp.tools.debug.ops`` (gdb-MI debug tooling).

Break-glass, queue-control, tuning, audit, inventory, and reconcile tools register through
their concrete modules in :mod:`kdive.mcp.app`.
"""

from __future__ import annotations
