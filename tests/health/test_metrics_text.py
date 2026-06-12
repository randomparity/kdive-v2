"""Prometheus text rendering tests for health metrics."""

from __future__ import annotations

from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    Histogram,
    HistogramDataPoint,
    Metric,
    MetricsData,
    ResourceMetrics,
    ScopeMetrics,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from kdive.health.metrics_text import render_prometheus


def test_histogram_renders_prometheus_bucket_sum_and_count_lines() -> None:
    metrics = MetricsData(
        resource_metrics=[
            ResourceMetrics(
                resource=Resource.create({}),
                scope_metrics=[
                    ScopeMetrics(
                        scope=InstrumentationScope("test"),
                        metrics=[
                            Metric(
                                name="kdive.request-duration",
                                description="Request duration",
                                unit="s",
                                data=Histogram(
                                    data_points=[
                                        HistogramDataPoint(
                                            attributes={
                                                "z_label": 'needs"escape',
                                                "a_label": "line\nbreak",
                                            },
                                            start_time_unix_nano=1,
                                            time_unix_nano=2,
                                            count=6,
                                            sum=1.75,
                                            bucket_counts=[1, 2, 3],
                                            explicit_bounds=[0.1, 1.0],
                                            min=0.05,
                                            max=1.4,
                                        )
                                    ],
                                    aggregation_temporality=AggregationTemporality.CUMULATIVE,
                                ),
                            )
                        ],
                        schema_url="",
                    )
                ],
                schema_url="",
            )
        ]
    )

    body = render_prometheus(metrics)

    assert "# TYPE kdive_request_duration histogram\n" in body
    assert (
        'kdive_request_duration_bucket{a_label="line\\nbreak",le="0.1",'
        'z_label="needs\\"escape"} 1\n'
    ) in body
    assert (
        'kdive_request_duration_bucket{a_label="line\\nbreak",le="1.0",'
        'z_label="needs\\"escape"} 3\n'
    ) in body
    assert (
        'kdive_request_duration_bucket{a_label="line\\nbreak",le="+Inf",'
        'z_label="needs\\"escape"} 6\n'
    ) in body
    assert (
        'kdive_request_duration_sum{a_label="line\\nbreak",z_label="needs\\"escape"} 1.75\n'
    ) in body
    assert (
        'kdive_request_duration_count{a_label="line\\nbreak",z_label="needs\\"escape"} 6\n'
    ) in body
