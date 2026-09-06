from __future__ import annotations

from datetime import datetime, timezone

from sonata_tasks.prometheus import (
    HttpPrometheusClient,
    PrometheusRetryPolicy,
    PrometheusSeries,
)


def sum_series_by_timestamp(series: tuple[PrometheusSeries, ...]) -> dict[float, float]:
    merged: dict[float, float] = {}
    for item in series:
        for sample in item.samples:
            merged[sample.timestamp] = merged.get(sample.timestamp, 0.0) + sample.value
    return merged


def _client(base_url: str) -> HttpPrometheusClient:
    return HttpPrometheusClient(base_url,
                                retry_policy=PrometheusRetryPolicy(
                                    attempts=3, backoff_seconds=2))


def query_prometheus_server_time(base_url: str, timeout_seconds: float = 20) -> float:
    return HttpPrometheusClient(base_url, timeout_seconds=timeout_seconds,
                                retry_policy=PrometheusRetryPolicy(
                                    attempts=3, backoff_seconds=2)).server_time()


def query_prometheus_range_series(base_url: str, metric_name: str, start: datetime,
                                  end: datetime, step_seconds: int = 2
                                  ) -> list[dict[str, float | str]]:
    merged = sum_series_by_timestamp(
        _client(base_url).query_range(metric_name, start, end, step_seconds)
    )
    return [{"timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
             "value": float(merged[timestamp])} for timestamp in sorted(merged)]
