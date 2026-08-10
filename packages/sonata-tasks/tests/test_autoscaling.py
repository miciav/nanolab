from __future__ import annotations

from dataclasses import dataclass

from sonata_tasks.loadtest.autoscaling import (
    HttpReplicaProbe,
    VerifyAutoscalingReplicas,
    VerifyInitialAutoscalingReplicas,
)


@dataclass
class _Result:
    return_code: int
    stdout: str
    stderr: str = ""


class _Runner:
    def __init__(self, values: list[str]) -> None:
        self.values = values
        self.commands: list[tuple[str, ...]] = []

    def run_vm_command(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        remote_dir: str | None,
        dry_run: bool,
    ):
        self.commands.append(argv)
        if not self.values:
            return _Result(return_code=0, stdout="0")
        return _Result(return_code=0, stdout=self.values.pop(0))


def test_http_replica_probe_reads_provider_neutral_status(monkeypatch) -> None:
    calls: list[str] = []

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"desiredReplicas": 3, "readyReplicas": 2}

    def get(url: str, *, timeout: float):
        calls.append(url)
        assert timeout == 4.0
        return Response()

    monkeypatch.setattr("sonata_tasks.loadtest.autoscaling.httpx.get", get)
    probe = HttpReplicaProbe(
        endpoint="http://127.0.0.1:8080/",
        function_name="word stats/java",
    )

    assert probe.desired_replicas() == 3
    assert probe.ready_replicas() == 2
    assert calls == [
        "http://127.0.0.1:8080/v1/functions/word%20stats%2Fjava/replicas",
        "http://127.0.0.1:8080/v1/functions/word%20stats%2Fjava/replicas",
    ]


def test_initial_autoscaling_replicas_requires_zero_desired_and_ready() -> None:
    class _Probe:
        def desired_replicas(self) -> int:
            return 0

        def ready_replicas(self) -> int:
            return 1

    try:
        VerifyInitialAutoscalingReplicas(probe=_Probe()).run()
    except RuntimeError as exc:
        assert "expected desired=0 and ready=0" in str(exc)
        return
    raise AssertionError("expected RuntimeError")


def test_verify_autoscaling_replicas_observes_scale_up_and_down(monkeypatch) -> None:
    monkeypatch.setattr("sonata_tasks.loadtest.autoscaling.time.sleep", lambda _: None)
    runner = _Runner(["1", "1", "2", "2", "0"])
    task = VerifyAutoscalingReplicas(
        task_id="autoscaling.verify_replicas",
        title="Verify autoscaling replicas",
        runner=runner,
        namespace="nanofaas",
        deployment_name="fn-word-stats-java",
        remote_dir="/home/ubuntu/mcFaas",
        scale_up_polls=2,
        scale_down_initial_delay_seconds=0,
        scale_down_polls=1,
        poll_interval_seconds=1,
    )

    summary = task.run()

    assert summary.max_replicas_observed == 2
    assert summary.final_desired_replicas == 0
    assert len(runner.commands) == 5
    assert all("kubectl" in " ".join(command) for command in runner.commands)


def test_verify_autoscaling_replicas_quotes_shell_arguments(monkeypatch) -> None:
    monkeypatch.setattr("sonata_tasks.loadtest.autoscaling.time.sleep", lambda _: None)
    runner = _Runner(["2", "2", "0"])
    task = VerifyAutoscalingReplicas(
        task_id="autoscaling.verify_replicas",
        title="Verify autoscaling replicas",
        runner=runner,
        namespace="nanofaas; touch /tmp/ns-pwned",
        deployment_name="fn-word-stats-java; touch /tmp/deploy-pwned",
        remote_dir="/home/ubuntu/mcFaas",
        scale_up_polls=1,
        scale_down_initial_delay_seconds=0,
        scale_down_polls=1,
        poll_interval_seconds=1,
    )

    task.run()

    command = runner.commands[0][2]
    assert "'fn-word-stats-java; touch /tmp/deploy-pwned'" in command
    assert "'nanofaas; touch /tmp/ns-pwned'" in command


def test_verify_autoscaling_replicas_accepts_scale_down_on_final_poll(monkeypatch) -> None:
    monkeypatch.setattr("sonata_tasks.loadtest.autoscaling.time.sleep", lambda _: None)
    runner = _Runner(["2", "2", "1", "0"])
    task = VerifyAutoscalingReplicas(
        task_id="autoscaling.verify_replicas",
        title="Verify autoscaling replicas",
        runner=runner,
        namespace="nanofaas",
        deployment_name="fn-word-stats-java",
        remote_dir="/home/ubuntu/mcFaas",
        scale_up_polls=1,
        scale_down_initial_delay_seconds=0,
        scale_down_polls=1,
        poll_interval_seconds=1,
    )

    summary = task.run()

    assert summary.final_desired_replicas == 0


def test_verify_autoscaling_replicas_fails_when_scale_up_never_exceeds_one(monkeypatch) -> None:
    monkeypatch.setattr("sonata_tasks.loadtest.autoscaling.time.sleep", lambda _: None)
    runner = _Runner(["1", "1", "1", "1"])
    task = VerifyAutoscalingReplicas(
        task_id="autoscaling.verify_replicas",
        title="Verify autoscaling replicas",
        runner=runner,
        namespace="nanofaas",
        deployment_name="fn-word-stats-java",
        remote_dir="/home/ubuntu/mcFaas",
        scale_up_polls=2,
        scale_down_initial_delay_seconds=0,
        scale_down_polls=1,
        poll_interval_seconds=1,
    )

    try:
        task.run()
    except RuntimeError as exc:
        assert "Scale-up not observed" in str(exc)
        return
    raise AssertionError("expected RuntimeError")


def test_verify_autoscaling_replicas_fails_when_scale_down_never_reaches_zero(monkeypatch) -> None:
    monkeypatch.setattr("sonata_tasks.loadtest.autoscaling.time.sleep", lambda _: None)
    runner = _Runner(["2", "2", "2", "2", "1"])
    task = VerifyAutoscalingReplicas(
        task_id="autoscaling.verify_replicas",
        title="Verify autoscaling replicas",
        runner=runner,
        namespace="nanofaas",
        deployment_name="fn-word-stats-java",
        remote_dir="/home/ubuntu/mcFaas",
        scale_up_polls=2,
        scale_down_initial_delay_seconds=0,
        scale_down_polls=1,
        poll_interval_seconds=1,
    )

    try:
        task.run()
    except RuntimeError as exc:
        assert "Scale-down to 0 not observed" in str(exc)
        return
    raise AssertionError("expected RuntimeError")


class _FailingRunner:
    def __init__(self, stderr: str, return_code: int = 1) -> None:
        self.stderr = stderr
        self.return_code = return_code

    def run_vm_command(self, argv, *, env, remote_dir, dry_run):
        return _Result(return_code=self.return_code, stdout="", stderr=self.stderr)


def test_replica_probe_reports_missing_deployment_clearly() -> None:
    from sonata_tasks.loadtest.autoscaling import ReplicaProbe

    probe = ReplicaProbe(
        runner=_FailingRunner('Error from server (NotFound): deployments.apps "fn-x" not found'),
        namespace="nanofaas",
        deployment_name="fn-x",
        remote_dir="/home/ubuntu/mcFaas",
    )
    try:
        probe.desired_replicas()
    except RuntimeError as exc:
        assert "not found" in str(exc)
        assert "fn-x" in str(exc)
        return
    raise AssertionError("expected RuntimeError")


def test_replica_probe_propagates_kubectl_errors() -> None:
    from sonata_tasks.loadtest.autoscaling import ReplicaProbe

    probe = ReplicaProbe(
        runner=_FailingRunner("Unable to connect to the server: dial tcp: lookup ..."),
        namespace="nanofaas",
        deployment_name="fn-word-stats-java",
        remote_dir="/home/ubuntu/mcFaas",
    )
    try:
        probe.ready_replicas()
    except RuntimeError as exc:
        assert "Unable to connect" in str(exc)
        return
    raise AssertionError("expected RuntimeError")


def test_replica_probe_treats_empty_jsonpath_output_as_zero() -> None:
    from sonata_tasks.loadtest.autoscaling import ReplicaProbe

    probe = ReplicaProbe(
        runner=_Runner([""]),  # readyReplicas is absent from status when 0
        namespace="nanofaas",
        deployment_name="fn-word-stats-java",
        remote_dir="/home/ubuntu/mcFaas",
    )
    assert probe.ready_replicas() == 0


def test_replica_watcher_records_max_while_running() -> None:
    from sonata_tasks.loadtest.autoscaling import ReplicaProbe, ReplicaWatcher

    runner = _Runner(["1", "1", "2", "3", "2", "1"])
    probe = ReplicaProbe(
        runner=runner,
        namespace="nanofaas",
        deployment_name="fn-word-stats-java",
        remote_dir="/home/ubuntu/mcFaas",
    )
    watcher = ReplicaWatcher(probe, poll_interval_seconds=0.01)

    watcher.start()
    import time as _time
    deadline = _time.time() + 2.0
    while watcher.max_observed < 3 and _time.time() < deadline:
        _time.sleep(0.01)
    watcher.stop()

    assert watcher.max_observed >= 3


def test_replica_watcher_survives_probe_errors() -> None:
    from sonata_tasks.loadtest.autoscaling import ReplicaProbe, ReplicaWatcher

    probe = ReplicaProbe(
        runner=_FailingRunner("Unable to connect to the server"),
        namespace="nanofaas",
        deployment_name="fn-word-stats-java",
        remote_dir="/home/ubuntu/mcFaas",
    )
    watcher = ReplicaWatcher(probe, poll_interval_seconds=0.01)
    watcher.start()
    import time as _time
    _time.sleep(0.05)
    watcher.stop()  # must not raise; errors recorded, watcher keeps sampling

    assert watcher.max_observed == 0


def test_verify_uses_watcher_max_and_skips_scale_up_polling(monkeypatch) -> None:
    monkeypatch.setattr("sonata_tasks.loadtest.autoscaling.time.sleep", lambda _: None)

    class _WatcherStub:
        max_observed = 3

    # Only the scale-down phase should hit kubectl: desired goes straight to 0.
    runner = _Runner(["0"])
    task = VerifyAutoscalingReplicas(
        task_id="autoscaling.verify_replicas",
        title="Verify autoscaling replicas",
        runner=runner,
        namespace="nanofaas",
        deployment_name="fn-word-stats-java",
        remote_dir="/home/ubuntu/mcFaas",
        scale_up_polls=2,
        scale_down_initial_delay_seconds=0,
        scale_down_polls=1,
        poll_interval_seconds=1,
        watcher=_WatcherStub(),
    )

    summary = task.run()

    assert summary.max_replicas_observed == 3
    assert summary.final_desired_replicas == 0
    assert task.result == summary
    # One kubectl call total (the final desired check), no scale-up polling.
    assert len(runner.commands) == 1


def test_verify_result_requires_a_completed_run(monkeypatch) -> None:
    task = VerifyAutoscalingReplicas(
        task_id="autoscaling.verify_replicas",
        title="Verify autoscaling replicas",
        runner=_Runner([]),
        namespace="nanofaas",
        deployment_name="fn-word-stats-java",
        remote_dir=".",
    )

    import pytest

    with pytest.raises(RuntimeError, match="has not been called"):
        _ = task.result


def test_scale_up_failure_message_includes_watcher_probe_errors(monkeypatch) -> None:
    monkeypatch.setattr("sonata_tasks.loadtest.autoscaling.time.sleep", lambda _: None)

    class _WatcherStub:
        max_observed = 0
        errors = ["Unable to connect to the server: dial tcp"]

    runner = _Runner(["0", "0"])
    task = VerifyAutoscalingReplicas(
        task_id="autoscaling.verify_replicas",
        title="Verify autoscaling replicas",
        runner=runner,
        namespace="nanofaas",
        deployment_name="fn-word-stats-java",
        remote_dir="/home/ubuntu/mcFaas",
        scale_up_polls=1,
        scale_down_initial_delay_seconds=0,
        scale_down_polls=1,
        poll_interval_seconds=1,
        watcher=_WatcherStub(),
    )

    try:
        task.run()
    except RuntimeError as exc:
        assert "Scale-up not observed" in str(exc)
        assert "Unable to connect" in str(exc)
        return
    raise AssertionError("expected RuntimeError")


def test_fetch_autoscaling_summary_creates_parent_and_fetches(tmp_path) -> None:
    from sonata_tasks.loadtest.autoscaling import FetchAutoscalingSummary

    fetched: list[tuple[str, object]] = []

    class _Fetcher:
        def fetch_from(self, remote, local):
            fetched.append((remote, local))

    local = tmp_path / "metrics" / "autoscaling-k6-summary.json"
    FetchAutoscalingSummary(
        task_id="autoscaling.fetch_summary",
        title="Fetch autoscaling k6 summary",
        fetcher=_Fetcher(),
        remote_path="/home/ubuntu/two-vm-loadtest/results/autoscaling-k6-summary.json",
        local_path=local,
    ).run()

    assert local.parent.is_dir()
    assert fetched == [("/home/ubuntu/two-vm-loadtest/results/autoscaling-k6-summary.json", local)]
