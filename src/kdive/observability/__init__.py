"""The kdive OpenTelemetry package (ADR-0090).

All OTel SDK wiring — including the pre-stable ``_logs`` signal — is confined to this
package, so an upstream API shift is a single-package change (§7). Process entrypoints
call :func:`kdive.observability.facade.bootstrap_stdout_floor` first (the
bootstrap-ordering invariant, §1) and :func:`kdive.observability.facade.init_telemetry`
once the config is loaded.
"""
