from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from nanolab.tasks.loadtest import prometheus
from sonata_tasks.prometheus import PrometheusSample, PrometheusSeries


def test_query_range_series_raises_on_http_error() -> None:
    with patch.object(prometheus, "_client") as client_factory:
        client_factory.return_value.query_range.side_effect = RuntimeError("connection refused")
        with pytest.raises(RuntimeError, match="connection refused"):
            prometheus.query_prometheus_range_series(
                "http://localhost:9090",
                "http_requests_total",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            )


def test_query_range_series_returns_parsed_points() -> None:
    mock_client = MagicMock()
    mock_client.query_range.return_value = (
        PrometheusSeries(
            {"__name__": "http_requests_total"},
            (PrometheusSample(1704067200.0, 42.0), PrometheusSample(1704067260.0, 43.0)),
        ),
    )

    with patch.object(prometheus, "_client", return_value=mock_client):
        result = prometheus.query_prometheus_range_series(
            "http://localhost:9090",
            "http_requests_total",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        )

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["value"] == 42.0


def test_query_prometheus_server_time_parses_time_scalar(monkeypatch) -> None:
    fake = MagicMock()
    fake.server_time.return_value = 1780728785.1
    monkeypatch.setattr(prometheus, "_client", lambda _url, _timeout=20: fake)
    assert prometheus.query_prometheus_server_time("http://prom") == 1780728785.1
