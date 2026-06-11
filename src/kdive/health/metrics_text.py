"""Render OTel ``MetricsData`` as Prometheus text exposition (ADR-0090 §5).

``/metrics`` is a Prometheus-style pull surface so a collector can scrape without OTLP.
Rather than take an ``opentelemetry-exporter-prometheus`` (and ``prometheus_client``)
dependency for the RED metric kinds the server emits, this renders the small subset the
middleware produces — sums (counters) and histograms — straight from the SDK's in-memory
``MetricsData``. Identifier-label scrubbing is handled upstream by the metric views /
the label allowlist (ADR-0090 §4); this module only serializes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from opentelemetry.sdk.metrics.export import (
    Histogram,
    HistogramDataPoint,
    MetricsData,
    NumberDataPoint,
    Sum,
)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def render_prometheus(metrics_data: MetricsData | None) -> str:
    """Return the Prometheus text exposition for ``metrics_data``.

    Sums render as a single sample per data point; histograms render the standard
    ``_bucket`` (cumulative, with a ``+Inf`` bucket), ``_sum``, and ``_count`` series.
    Unknown metric kinds are skipped (the server emits only sums and histograms). A
    ``None`` reader result (no metrics collected yet) renders to an empty body.
    """
    if metrics_data is None:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for metric in _iter_metrics(metrics_data):
        data = metric.data
        name = _sanitize(metric.name)
        if isinstance(data, Sum):
            _emit_help(lines, seen, name, "counter", metric)
            _render_sum(lines, name, data.data_points)
        elif isinstance(data, Histogram):
            _emit_help(lines, seen, name, "histogram", metric)
            _render_histogram(lines, name, data.data_points)
    return "".join(f"{line}\n" for line in lines)


def _iter_metrics(metrics_data: MetricsData) -> Iterable[Any]:
    for resource_metric in metrics_data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            yield from scope_metric.metrics


def _emit_help(lines: list[str], seen: set[str], name: str, kind: str, metric: Any) -> None:
    if name in seen:
        return
    seen.add(name)
    if metric.description:
        lines.append(f"# HELP {name} {metric.description}")
    lines.append(f"# TYPE {name} {kind}")


def _render_sum(lines: list[str], name: str, points: list[NumberDataPoint]) -> None:
    for point in points:
        lines.append(f"{name}{_labels(point.attributes)} {_num(point.value)}")


def _render_histogram(lines: list[str], name: str, points: list[HistogramDataPoint]) -> None:
    for point in points:
        cumulative = 0
        for bound, count in zip(point.explicit_bounds, point.bucket_counts, strict=False):
            cumulative += count
            lines.append(f"{name}_bucket{_labels(point.attributes, le=_num(bound))} {cumulative}")
        lines.append(f"{name}_bucket{_labels(point.attributes, le='+Inf')} {point.count}")
        lines.append(f"{name}_sum{_labels(point.attributes)} {_num(point.sum)}")
        lines.append(f"{name}_count{_labels(point.attributes)} {point.count}")


def _labels(attributes: Mapping[str, Any] | None, *, le: str | None = None) -> str:
    pairs = [(str(k), _escape(str(v))) for k, v in (attributes or {}).items()]
    if le is not None:
        pairs.append(("le", le))
    if not pairs:
        return ""
    body = ",".join(f'{k}="{v}"' for k, v in sorted(pairs))
    return "{" + body + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _sanitize(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_:" else "_" for ch in name)


def _num(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    return repr(float(value))
