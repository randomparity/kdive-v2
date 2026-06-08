"""Platform control-plane MCP tools (`ops.*`, ADR-0062).

The `ops.*` namespace holds platform-operator control-plane actions (reconcile, queue
control, capacity/cost tuning) and platform-admin break-glass. Each tool gates on the
M1.1 ``require_platform_role`` seam and audits cross-tenant actions. (Unrelated to
``tools/debug/ops.py``, which is gdb-MI tooling.)
"""

from __future__ import annotations
