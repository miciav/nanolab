from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

# Prometheus itself answers a range query over a whole run in well under a
# millisecond — measured on the VM, 0.7ms. Every second spent here is the
# transport: on a remote provider these queries cross an SSH tunnel, and a tunnel
# that stalls or is re-establishing eats the budget without Prometheus ever being
# asked. A snapshot arriving a few seconds late costs nothing; a required query
# giving up loses the whole cell, which is eight minutes of load.
_DEFAULT_TIMEOUT_S = 20.0
# Reads are idempotent, so retrying is safe in a way that retrying a load test is
# not. This mirrors the release path's `retry_on_connection_death`, and for the
# same stated reason: only the caller knows whether an operation may be re-run,
# and a query plainly may.
_ATTEMPTS = 3
_BACKOFF_S = 2.0


def _prometheus_api_get(
    base_url: str,
    path: str,
    params: dict[str, str],
    timeout_seconds: float = _DEFAULT_TIMEOUT_S,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    last: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            response = httpx.get(url, params=params, timeout=timeout_seconds)
            data = response.json()
        except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
            last = exc
            if attempt < _ATTEMPTS:
                sleep(_BACKOFF_S)
            continue
        if data.get("status") != "success":
            # A well-formed refusal is Prometheus answering, not the transport
            # failing: a bad query returns the same way every time.
            raise RuntimeError(f"prometheus api failed for {path}: {data}")
        return data.get("data")
    raise RuntimeError(f"prometheus api request failed for {path}: {last}")


def query_prometheus_server_time(
    base_url: str, timeout_seconds: float = _DEFAULT_TIMEOUT_S
) -> float:
    """Return Prometheus's current evaluation time (epoch seconds) via ``time()``.

    Used to align query windows to Prometheus's clock: when the host (which builds
    the window from wall-clock) and the metrics-source VM (which timestamps the
    samples) have drifted — e.g. the host slept mid-run — a host-clock window
    misses the VM-clock samples. ``time()`` reports Prometheus's own clock.
    """
    data = _prometheus_api_get(base_url, "/api/v1/query", {"query": "time()"}, timeout_seconds)
    # Scalar query: data == {"resultType": "scalar", "result": [<eval_ts>, "<value>"]}
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, list) and len(result) == 2:
        try:
            return float(result[1])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid prometheus time() value: {result!r}") from exc
    raise RuntimeError(f"unexpected prometheus time() result: {result!r}")


def _coerce_sample(sample: Any) -> tuple[float, float] | None:
    """Return ``(timestamp, value)`` floats, or ``None`` when unparseable."""
    if not isinstance(sample, list) or len(sample) != 2:
        return None
    raw_ts, raw_value = sample
    try:
        return float(raw_ts), float(raw_value)
    except (TypeError, ValueError):
        return None


def _merge_samples(result: list[Any]) -> dict[float, float]:
    # Merge samples across label dimensions by timestamp.
    merged: dict[float, float] = {}
    for series in result:
        if not isinstance(series, dict):
            continue
        values = series.get("values", [])
        if not isinstance(values, list):
            continue
        for sample in values:
            parsed = _coerce_sample(sample)
            if parsed is None:
                continue
            ts, value = parsed
            merged[ts] = merged.get(ts, 0.0) + value
    return merged


def query_prometheus_range_series(
    base_url: str,
    metric_name: str,
    start: datetime,
    end: datetime,
    step_seconds: int = 2,
) -> list[dict[str, float | str]]:
    data = _prometheus_api_get(
        base_url,
        "/api/v1/query_range",
        {
            "query": metric_name,
            "start": str(start.timestamp()),
            "end": str(end.timestamp()),
            "step": f"{step_seconds}s",
        },
    )
    if not isinstance(data, dict):
        raise RuntimeError("invalid prometheus query_range payload")
    result = data.get("result", [])
    if not isinstance(result, list):
        raise RuntimeError("invalid prometheus query_range payload")

    merged = _merge_samples(result)

    points: list[dict[str, float | str]] = []
    for timestamp in sorted(merged):
        iso = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        points.append({"timestamp": iso, "value": float(merged[timestamp])})
    return points
