"""The snapshot must survive a stalled tunnel, and must not paper over a bad query."""

from __future__ import annotations

import pytest
from sonata_tasks.prometheus import PrometheusRetryPolicy

from nanolab.tasks.loadtest import prometheus as prom


def test_the_product_client_configures_bounded_retries() -> None:
    """Prometheus answers a range query in under a millisecond, measured on the VM.

    Seconds spent here are the SSH tunnel, not the database, and a tunnel that
    stalls once should not cost the eight minutes of load the cell just ran.
    """
    client = prom._client("http://p")

    assert client._retry == PrometheusRetryPolicy(attempts=3, backoff_seconds=2)


def test_server_time_delegates_to_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def server_time(self) -> float:
            return 1780728785.1

    monkeypatch.setattr(prom, "_client", lambda _url, _timeout=20: FakeClient())

    assert prom.query_prometheus_server_time("http://p") == 1780728785.1


def test_range_queries_delegate_without_hiding_protocol_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed query fails the same way every time; retrying only wastes the budget."""
    class RefusingClient:
        def query_range(self, *args: object) -> object:
            raise RuntimeError("prometheus query failed: bad query")

    monkeypatch.setattr(prom, "_client", lambda _url: RefusingClient())

    with pytest.raises(RuntimeError, match="bad query"):
        prom.query_prometheus_range_series(
            "http://p", "up", prom.datetime.now(prom.timezone.utc), prom.datetime.now(prom.timezone.utc)
        )


def test_a_timeout_says_it_means_unreachable() -> None:
    """The word "timeout" reads as "slow", and this endpoint is never slow.

    Measured on the VM, a range query over a whole run answers in 0.7ms. A
    timeout is a firewall dropping packets — which on Azure meant the operator's
    address had changed since the VM was created, and cost a wrong diagnosis
    about SSH tunnels before anyone thought to compare the two addresses.
    """
    from nanolab.tasks.loadtest.tasks import _unreachable_hint

    hint = _unreachable_hint(RuntimeError("prometheus api request failed: timed out"))

    assert "unreachable rather than slow" in hint
    assert "operator address" in hint


def test_other_failures_get_no_hint() -> None:
    """A malformed query is not a reachability problem and must not be dressed as one."""
    from nanolab.tasks.loadtest.tasks import _unreachable_hint

    assert _unreachable_hint(RuntimeError("prometheus api failed: bad_data")) == ""
