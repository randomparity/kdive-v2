"""The single OpenTelemetry facade for a kdive process (ADR-0090).

All OTel SDK wiring lives here so an upstream API shift (notably the pre-stable
``_logs`` signal, §7) is a single-package change. The facade owns:

- the bootstrap-ordering invariant (§1): a stdlib stdout JSON floor installed *first*,
  before the providers/config/clients, so early-startup records (config-validation
  failures) are never lost; the OTel handler is added once the provider is built and
  the stdlib floor handler is then removed so stdout never doubles;
- one ``LoggerProvider`` / ``TracerProvider`` / ``MeterProvider`` per process (§1);
- dual log export (§2): the always-on stdout JSON exporter (ADR-0014 schema + additive
  ``trace_id``/``span_id``) and an OTLP exporter that is **default-off**, on only under
  ``KDIVE_OTEL_*``;
- the three redacting processors (§4) and the identifier-label allowlist;
- non-blocking, drop-not-block OTLP batch queues with a drop self-metric (§6);
- parent-based ratio trace sampling (§2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_NAMESPACE, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

import kdive.config as config
from kdive.config.core_settings import (
    OTEL_ENABLED,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_SERVICE_NAMESPACE,
    OTEL_TRACES_SAMPLER_RATIO,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import ContextFilter, configure_logging, remove_stdlib_floor
from kdive.observability.redaction import (
    RedactingLogProcessor,
    RedactingMetricExporter,
    RedactingSpanExporter,
)
from kdive.observability.stdout_exporter import StdoutJsonLogExporter
from kdive.security.secrets.secret_registry import SecretRegistry

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def otlp_enabled() -> bool:
    """Return whether OTLP export is enabled (``KDIVE_OTEL_ENABLED`` truthy)."""
    raw = config.get(OTEL_ENABLED)
    return bool(raw) and raw.strip().lower() in _TRUTHY


def require_otlp_endpoint() -> str:
    """Return the configured OTLP endpoint, failing fast when OTLP is on but unset."""
    endpoint = config.get(OTEL_EXPORTER_OTLP_ENDPOINT)
    if not endpoint:
        raise CategorizedError(
            "KDIVE_OTEL_ENABLED is set but KDIVE_OTEL_EXPORTER_OTLP_ENDPOINT is not",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "variable": OTEL_EXPORTER_OTLP_ENDPOINT.name,
                "suggest": OTEL_EXPORTER_OTLP_ENDPOINT.suggest,
            },
        )
    return endpoint


@dataclass(frozen=True, slots=True)
class Telemetry:
    """The per-process OTel providers, returned by :func:`init_telemetry`."""

    logger_provider: LoggerProvider
    tracer_provider: TracerProvider
    meter_provider: MeterProvider


def bootstrap_stdout_floor(level: str, *, secret_registry: SecretRegistry) -> None:
    """Install the stdlib JSON stdout floor — the *first* startup step (ADR-0090 §1).

    Delegates to the ADR-0014 stdlib configurator (no OTel dependency) so records
    emitted before the provider is built — including config-validation failures — are
    never lost to an unconfigured root logger.
    """
    configure_logging(level, secret_registry=secret_registry)


def _resource(service_name: str) -> Resource:
    namespace = config.get(OTEL_SERVICE_NAMESPACE) or "kdive"
    return Resource.create({SERVICE_NAME: service_name, SERVICE_NAMESPACE: namespace})


def _sampler() -> ParentBased:
    ratio = config.get(OTEL_TRACES_SAMPLER_RATIO)
    return ParentBased(root=TraceIdRatioBased(ratio if ratio is not None else 0.1))


def init_telemetry(service_name: str, *, secret_registry: SecretRegistry, level: str) -> Telemetry:
    """Build the three providers, bridge logging, and remove the stdlib floor.

    Args:
        service_name: The ``service.name`` resource attribute (e.g. the process name).
        secret_registry: The app-owned registry the redacting processors snapshot.
        level: The logging level for the bridged root logger.

    Returns:
        The constructed :class:`Telemetry` providers, registered process-globally.
    """
    resource = _resource(service_name)
    meter_provider = _build_meter_provider(resource, secret_registry)
    metrics.set_meter_provider(meter_provider)
    tracer_provider = _build_tracer_provider(resource, secret_registry, meter_provider)
    trace.set_tracer_provider(tracer_provider)
    logger_provider = _build_logger_provider(resource, secret_registry, meter_provider)
    set_logger_provider(logger_provider)
    _bridge_root_logger(logger_provider, level)
    return Telemetry(logger_provider, tracer_provider, meter_provider)


def _build_logger_provider(
    resource: Resource, secret_registry: SecretRegistry, meter_provider: MeterProvider
) -> LoggerProvider:
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(RedactingLogProcessor(secret_registry))
    provider.add_log_record_processor(SimpleLogRecordProcessor(StdoutJsonLogExporter()))
    if otlp_enabled():
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=require_otlp_endpoint()),
                meter_provider=meter_provider,
            )
        )
    return provider


def _build_tracer_provider(
    resource: Resource, secret_registry: SecretRegistry, meter_provider: MeterProvider
) -> TracerProvider:
    provider = TracerProvider(resource=resource, sampler=_sampler())
    if otlp_enabled():
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(
                RedactingSpanExporter(
                    OTLPSpanExporter(endpoint=require_otlp_endpoint()), secret_registry
                ),
                meter_provider=meter_provider,
            )
        )
    return provider


def _build_meter_provider(resource: Resource, secret_registry: SecretRegistry) -> MeterProvider:
    readers: list[PeriodicExportingMetricReader] = []
    if otlp_enabled():
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

        readers.append(
            PeriodicExportingMetricReader(
                RedactingMetricExporter(
                    OTLPMetricExporter(endpoint=require_otlp_endpoint()), secret_registry
                )
            )
        )
    return MeterProvider(resource=resource, metric_readers=readers)


def _bridge_root_logger(logger_provider: LoggerProvider, level: str) -> None:
    """Replace the stdlib stdout floor with the OTel ``LoggingHandler`` bridge (§1, §2).

    The OTel log pipeline now carries the stdout JSON exporter, so the stdlib floor
    handler is removed to keep stdout from doubling. The ``ContextFilter`` rides on the
    bridge so ``bind_context`` fields reach the OTel record as attributes (§3).
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = LoggingHandler(logger_provider=logger_provider)
    handler.addFilter(ContextFilter())
    remove_stdlib_floor()
    root.addHandler(handler)
