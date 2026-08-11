from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sonata_tasks.loadtest.models import K6Config, K6Stage, PrometheusQuery, TimeWindow
from sonata_tasks.loadtest.tasks import (
    CapturePrometheusSnapshot,
    FetchVmResults,
    WriteK6Report,
    WriteLoadtestSummary,
)
from sonata_tasks.k6 import K6Task
from sonata_engine import Resource, TaskInputs, Workflow
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult


class _Executor:
    def __init__(self, return_code: int = 0) -> None:
        self.spec: CommandTaskSpec | None = None
        self.return_code = return_code

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.spec = task
        return TaskResult(task_id="", status="passed", return_code=self.return_code)


def test_k6_task_is_a_role_bound_sonata_command(tmp_path: Path) -> None:
    executor = _Executor()
    config = _make_k6_config(tmp_path)

    outcome = K6Task(config, executor=executor, role="stack", remote_dir=".").run(TaskInputs.empty())

    assert executor.spec is not None
    assert executor.spec.argv[:2] == ("k6", "run")
    assert executor.spec.remote_dir == "."
    assert executor.spec.expected_exit_codes == frozenset({0, 99})
    assert "--summary-trend-stats" in executor.spec.argv
    assert outcome.value is not None
    assert outcome.value.passed is True


def test_k6_task_keeps_a_threshold_failure_as_an_outcome(tmp_path: Path) -> None:
    executor = _Executor(return_code=99)

    outcome = K6Task(_make_k6_config(tmp_path), executor=executor, role="stack").run(
        TaskInputs.empty()
    )

    assert outcome.value is not None
    assert outcome.value.passed is False


def test_k6_task_resolves_a_resource_target_url(tmp_path: Path) -> None:
    endpoint: Resource[str] = Resource(
        title="Control plane", acquire=lambda _inputs: "http://10.0.0.1:30080", release=lambda *_: None
    )
    executor = _Executor()
    workflow = Workflow(workflow_id="k6")
    workflow.add(
        K6Task(
            K6Config(
                script_path=Path("/remote/scripts/test.js"),
                target_url=endpoint,
                summary_output_path=Path("/remote/results/summary.json"),
            ),
            executor=executor,
            role="stack",
        ),
        requires=(endpoint,),
    )

    workflow.run()

    assert executor.spec is not None
    assert "NANOFAAS_URL=http://10.0.0.1:30080" in executor.spec.argv


def _make_k6_config(tmp_path: Path) -> K6Config:
    return K6Config(
        script_path=Path("/remote/scripts/test.js"),
        target_url="http://10.0.0.1:8080",
        summary_output_path=Path("/remote/results/summary.json"),
        stages=(K6Stage(duration="30s", target=5),),
        env={"NANOFAAS_FUNCTION": "my-fn"},
    )


# ---------------------------------------------------------------------------
# Helpers for new tasks
# ---------------------------------------------------------------------------


class _RecordingFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def fetch_from(self, remote: str, local: Path) -> None:
        self.calls.append((remote, local))


class _RecordingPrometheusClient:
    def __init__(self, points: list[dict] | None = None) -> None:
        self._points = points or [{"timestamp": "2026-01-01T10:00:00Z", "value": 1.0}]
        self.calls: list[tuple[str, TimeWindow, int]] = []

    def query_range(
        self, expr: str, window: TimeWindow, step_seconds: int = 5
    ) -> list[dict]:
        self.calls.append((expr, window, step_seconds))
        return self._points


def _make_window() -> TimeWindow:
    return TimeWindow(
        start=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# FetchVmResults tests
# ---------------------------------------------------------------------------


def test_fetch_vm_results_calls_fetcher(tmp_path: Path) -> None:
    fetcher = _RecordingFetcher()
    task = FetchVmResults(
        task_id="loadgen.fetch_results",
        title="Fetch results",
        fetcher=fetcher,
        remote_source="/remote/results",
        local_dest=tmp_path / "results",
    )
    returned = task.run()
    assert fetcher.calls == [("/remote/results", tmp_path / "results")]
    assert returned == tmp_path / "results"


def test_fetch_vm_results_hands_the_fetcher_an_absolute_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetchers shell out with their own working directory, so a relative
    destination lands somewhere else entirely — and the transfer still reports
    success, leaving the run directory empty."""
    monkeypatch.chdir(tmp_path)
    fetcher = _RecordingFetcher()
    task = FetchVmResults(
        task_id="loadgen.fetch_results",
        title="Fetch results",
        fetcher=fetcher,
        remote_source="/remote/results",
        local_dest=Path("relative/results"),
    )

    returned = task.run()

    ((_, destination),) = fetcher.calls
    assert destination.is_absolute()
    assert destination == (tmp_path / "relative/results").resolve()
    assert returned == destination


def test_fetch_vm_results_creates_local_dest(tmp_path: Path) -> None:
    fetcher = _RecordingFetcher()
    dest = tmp_path / "deep" / "nested" / "results"
    task = FetchVmResults(
        task_id="loadgen.fetch_results",
        title="Fetch results",
        fetcher=fetcher,
        remote_source="/remote/results",
        local_dest=dest,
    )
    task.run()
    assert dest.exists()


# ---------------------------------------------------------------------------
# CapturePrometheusSnapshot tests
# ---------------------------------------------------------------------------


def test_capture_prometheus_snapshot_queries_all_metrics(tmp_path: Path) -> None:
    client = _RecordingPrometheusClient()
    queries = (
        PrometheusQuery(name="req_total", expr="sum(http_requests_total)"),
        PrometheusQuery(name="latency", expr="http_req_duration"),
    )
    task = CapturePrometheusSnapshot(
        task_id="metrics.snapshot",
        title="Capture snapshots",
        client=client,
        queries=queries,
        window=_make_window(),
        output_dir=tmp_path,
    )
    task.run()
    queried_exprs = [call[0] for call in client.calls]
    assert "sum(http_requests_total)" in queried_exprs
    assert "http_req_duration" in queried_exprs


def test_capture_prometheus_snapshot_writes_json(tmp_path: Path) -> None:
    client = _RecordingPrometheusClient()
    task = CapturePrometheusSnapshot(
        task_id="metrics.snapshot",
        title="Capture snapshots",
        client=client,
        queries=(PrometheusQuery(name="req", expr="http_requests_total"),),
        window=_make_window(),
        output_dir=tmp_path,
    )
    dest = task.run()
    assert dest.exists()
    data = json.loads(dest.read_text())
    assert "queries" in data
    assert "req" in data["queries"]


def test_capture_prometheus_snapshot_accepts_callable_window(tmp_path: Path) -> None:
    client = _RecordingPrometheusClient()
    called: list[bool] = []

    def lazy_window() -> TimeWindow:
        called.append(True)
        return _make_window()

    task = CapturePrometheusSnapshot(
        task_id="metrics.snapshot",
        title="Capture snapshots",
        client=client,
        queries=(PrometheusQuery(name="req", expr="http_requests_total"),),
        window=lazy_window,
        output_dir=tmp_path,
    )
    task.run()
    assert called == [True]


def test_capture_prometheus_snapshot_raises_when_required_query_fails(tmp_path: Path) -> None:
    class _FailingClient:
        def query_range(self, expr: str, window: TimeWindow, step_seconds: int = 5) -> list[dict]:
            raise RuntimeError("prometheus unreachable")

    task = CapturePrometheusSnapshot(
        task_id="metrics.snapshot",
        title="Capture snapshots",
        client=_FailingClient(),
        queries=(PrometheusQuery(name="critical_metric", expr="some_metric", required=True),),
        window=_make_window(),
        output_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="critical_metric"):
        task.run()


def test_capture_prometheus_snapshot_raises_when_required_query_returns_empty(tmp_path: Path) -> None:
    class _EmptyClient:
        def query_range(self, expr: str, window: TimeWindow, step_seconds: int = 5) -> list[dict]:
            return []

    task = CapturePrometheusSnapshot(
        task_id="metrics.snapshot",
        title="Capture snapshots",
        client=_EmptyClient(),
        queries=(PrometheusQuery(name="critical_metric", expr="some_metric", required=True),),
        window=_make_window(),
        output_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="critical_metric"):
        task.run()


# ---------------------------------------------------------------------------
# WriteK6Report tests
# ---------------------------------------------------------------------------


def test_write_k6_report_generates_html(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    summary = {
        "metrics": {
            "http_req_duration": {
                "type": "trend",
                "values": {"avg": 123.4, "p(90)": 200.5, "p(95)": 350.2},
            },
            "http_reqs": {
                "type": "counter",
                "values": {"count": 1000, "rate": 10.5},
            },
        }
    }
    (data_dir / "k6-summary.json").write_text(json.dumps(summary), encoding="utf-8")

    task = WriteK6Report(
        task_id="loadtest.write_report",
        title="Write report",
        data_dir=data_dir,
        output_dir=tmp_path,
    )
    report_path = task.run()
    assert report_path.exists()
    html = report_path.read_text()
    assert "http_req_duration" in html
    assert "http_reqs" in html


def test_write_k6_report_renders_current_flat_k6_summary(tmp_path: Path) -> None:
    (tmp_path / "k6-summary.json").write_text(
        json.dumps(
            {
                "metrics": {
                    "http_req_duration": {
                        "avg": 12.5,
                        "med": 8.0,
                        "p(95)": 25.75,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    html = WriteK6Report(
        task_id="loadtest.write_report",
        title="Write report",
        data_dir=tmp_path,
        output_dir=tmp_path,
    ).run().read_text(encoding="utf-8")

    assert "avg: 12.5" in html
    assert "p(95): 25.8" in html


def test_write_k6_report_includes_prometheus_section_when_snapshot_present(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "metrics").mkdir(parents=True)
    (data_dir / "k6-summary.json").write_text(json.dumps({"metrics": {}}), encoding="utf-8")
    snapshot = {
        "queries": {
            "function_dispatch_total": {"points": [{"timestamp": "t", "value": 1.0}]}
        }
    }
    (data_dir / "metrics" / "prometheus-snapshot.json").write_text(
        json.dumps(snapshot), encoding="utf-8"
    )

    task = WriteK6Report(
        task_id="loadtest.write_report",
        title="Write report",
        data_dir=data_dir,
        output_dir=tmp_path,
    )
    html = task.run().read_text()
    assert "function_dispatch_total" in html


def test_write_k6_report_works_without_prometheus_snapshot(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "k6-summary.json").write_text(json.dumps({"metrics": {}}), encoding="utf-8")

    task = WriteK6Report(
        task_id="loadtest.write_report",
        title="Write report",
        data_dir=data_dir,
        output_dir=tmp_path,
    )
    report_path = task.run()
    assert report_path.exists()


def test_write_loadtest_summary_combines_k6_prometheus_and_autoscaling(
    tmp_path: Path,
) -> None:
    from sonata_tasks.loadtest.autoscaling import AutoscalingSummary

    (tmp_path / "metrics").mkdir()
    (tmp_path / "k6-summary.json").write_text(
        json.dumps(
            {
                "metrics": {
                    "http_reqs": {"count": 100, "rate": 10.0},
                    "http_req_failed": {"value": 0.01},
                    "http_req_duration": {"avg": 12.0, "p(95)": 25.0},
                    "checks": {"passes": 99, "fails": 1},
                    "ignored": {"value": 7},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "metrics" / "prometheus-snapshot.json").write_text(
        json.dumps(
            {
                "queries": {
                    "function_dispatch_total": {
                        "points": [
                            {"timestamp": "t0", "value": 2.0},
                            {"timestamp": "t1", "value": 102.0},
                        ]
                    },
                    "missing_optional": {"points": []},
                }
            }
        ),
        encoding="utf-8",
    )

    class _Autoscaling:
        result = AutoscalingSummary("fn-word-stats-java", 4, 0)

    destination = WriteLoadtestSummary(
        task_id="loadtest.write_summary",
        title="Write load-test summary",
        data_dir=tmp_path,
        output_dir=tmp_path,
        autoscaling=_Autoscaling(),
    ).run()
    summary = json.loads(destination.read_text(encoding="utf-8"))

    assert summary["schema_version"] == 1
    assert summary["k6"]["http_reqs"]["count"] == 100
    assert "ignored" not in summary["k6"]
    assert summary["prometheus"]["function_dispatch_total"] == {
        "points": 2,
        "first": 2.0,
        "last": 102.0,
        "delta": 100.0,
        "min": 2.0,
        "max": 102.0,
    }
    assert summary["prometheus"]["missing_optional"] == {"points": 0}
    assert summary["autoscaling"] == {
        "deployment_name": "fn-word-stats-java",
        "max_replicas_observed": 4,
        "final_desired_replicas": 0,
    }


class _ClockOffsetPrometheusClient:
    """Prometheus client whose clock is offset from the host (simulates VM drift)."""

    def __init__(self, offset_seconds: float) -> None:
        self._offset = offset_seconds
        self.calls: list[tuple[str, TimeWindow, int]] = []

    def server_time(self) -> float:
        return datetime.now(timezone.utc).timestamp() + self._offset

    def query_range(self, expr: str, window: TimeWindow, step_seconds: int = 5) -> list[dict]:
        self.calls.append((expr, window, step_seconds))
        return [{"timestamp": "t", "value": 1.0}]


def test_capture_prometheus_snapshot_shifts_window_to_prometheus_clock(tmp_path: Path) -> None:
    offset = -1500.0  # Prometheus/VM clock 25 min behind the host (host slept mid-run)
    client = _ClockOffsetPrometheusClient(offset)
    window = _make_window()
    task = CapturePrometheusSnapshot(
        task_id="metrics.snapshot",
        title="Capture snapshots",
        client=client,
        queries=(PrometheusQuery(name="dispatch", expr="function_dispatch_total", required=True),),
        window=window,
        output_dir=tmp_path,
    )
    task.run()  # must NOT raise: window is aligned so the required query finds data
    queried = client.calls[0][1]
    assert abs(queried.start.timestamp() - (window.start.timestamp() + offset)) <= 60
    assert abs(queried.end.timestamp() - (window.end.timestamp() + offset)) <= 60


def test_capture_prometheus_snapshot_adds_scrape_margin_without_server_time(tmp_path: Path) -> None:
    client = _RecordingPrometheusClient()
    window = _make_window()
    task = CapturePrometheusSnapshot(
        task_id="metrics.snapshot",
        title="Capture snapshots",
        client=client,
        queries=(PrometheusQuery(name="req", expr="http_requests_total"),),
        window=window,
        output_dir=tmp_path,
    )
    task.run()
    queried = client.calls[0][1]
    assert (window.start - queried.start).total_seconds() == 30
    assert (queried.end - window.end).total_seconds() == 30
