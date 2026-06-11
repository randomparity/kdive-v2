"""The kdive OpenTelemetry facade (ADR-0090).

All OTel SDK wiring — including the pre-stable ``_logs`` signal — is confined to this
package, so an upstream API shift is a single-package change (§7). Process entrypoints
call :func:`bootstrap_stdout_floor` first (the bootstrap-ordering invariant, §1) and
:func:`init_telemetry` once the config is loaded.
"""

from __future__ import annotations

from kdive.observability.facade import (
    Telemetry,
    bootstrap_stdout_floor,
    init_telemetry,
    otlp_enabled,
    require_otlp_endpoint,
)

__all__ = [
    "Telemetry",
    "bootstrap_stdout_floor",
    "init_telemetry",
    "otlp_enabled",
    "require_otlp_endpoint",
]
