"""The snapshot must survive a stalled tunnel, and must not paper over a bad query."""

from __future__ import annotations

import httpx
import pytest

from sonata_tasks.loadtest import prometheus as prom


def test_a_transport_failure_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prometheus answers a range query in under a millisecond, measured on the VM.

    Seconds spent here are the SSH tunnel, not the database, and a tunnel that
    stalls once should not cost the eight minutes of load the cell just ran.
    """
    calls = {"n": 0}

    def flaky(url, params=None, timeout=None):  # noqa: ANN001, ARG001
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"status": "success", "data": {"ok": True}})

    monkeypatch.setattr(prom.httpx, "get", flaky)

    result = prom._prometheus_api_get("http://p", "/api/v1/query", {}, sleep=lambda _: None)

    assert result == {"ok": True}
    assert calls["n"] == 3


def test_it_gives_up_after_a_bounded_number_of_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def always_fails(url, params=None, timeout=None):  # noqa: ANN001, ARG001
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(prom.httpx, "get", always_fails)

    with pytest.raises(RuntimeError, match="prometheus api request failed"):
        _ = prom._prometheus_api_get("http://p", "/api/v1/query", {}, sleep=lambda _: None)

    assert calls["n"] == prom._ATTEMPTS


def test_a_refused_query_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed query fails the same way every time; retrying only wastes the budget."""
    calls = {"n": 0}

    def refuses(url, params=None, timeout=None):  # noqa: ANN001, ARG001
        calls["n"] += 1
        return httpx.Response(200, json={"status": "error", "error": "bad query"})

    monkeypatch.setattr(prom.httpx, "get", refuses)

    with pytest.raises(RuntimeError, match="prometheus api failed"):
        _ = prom._prometheus_api_get("http://p", "/api/v1/query", {}, sleep=lambda _: None)

    assert calls["n"] == 1


def test_a_timeout_says_it_means_unreachable() -> None:
    """The word "timeout" reads as "slow", and this endpoint is never slow.

    Measured on the VM, a range query over a whole run answers in 0.7ms. A
    timeout is a firewall dropping packets — which on Azure meant the operator's
    address had changed since the VM was created, and cost a wrong diagnosis
    about SSH tunnels before anyone thought to compare the two addresses.
    """
    from sonata_tasks.loadtest.tasks import _unreachable_hint

    hint = _unreachable_hint(RuntimeError("prometheus api request failed: timed out"))

    assert "unreachable rather than slow" in hint
    assert "operator address" in hint


def test_other_failures_get_no_hint() -> None:
    """A malformed query is not a reachability problem and must not be dressed as one."""
    from sonata_tasks.loadtest.tasks import _unreachable_hint

    assert _unreachable_hint(RuntimeError("prometheus api failed: bad_data")) == ""
