from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sonata_engine import TaskInputs, TaskOutcome, Workflow
from sonata_engine.errors import NoUpstreamValueError
from sonata_engine.workflow.context import bind_workflow_sink
from sonata_tasks.loadtest.autoscaling import AutoscalingSummary
from sonata_tasks.loadtest.models import K6RunResult, TimeWindow
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.loadtest import (
    CapturePrometheusTask,
    EvaluateGateTask,
    LoadtestOutcome,
    RunK6Task,
    VerifyAutoscalingTask,
    WriteReportTask,
    WriteSummaryTask,
    loadtest_composite,
)

STARTED = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
ENDED = datetime(2026, 7, 28, 10, 5, tzinfo=UTC)


def _k6(passed: bool = True) -> K6RunResult:
    return K6RunResult(
        summary_path=Path("k6-summary.json"), started_at=STARTED, ended_at=ENDED, passed=passed
    )


def _inputs(upstream: object) -> TaskInputs:
    return replace(TaskInputs.empty(), _upstream=upstream)


@dataclass
class FakeRunK6:
    result_value: K6RunResult = field(default_factory=_k6)
    calls: int = 0

    def run(self, inputs: TaskInputs) -> TaskOutcome[K6RunResult]:
        del inputs
        self.calls += 1
        return TaskOutcome(value=self.result_value)


@dataclass
class FakeWatcher:
    events: list[str] = field(default_factory=list[str])

    def start(self) -> None:
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list[CommandTaskSpec])

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        # The platform half resolves the control plane's address before anything
        # can register against it.
        stdout = "10.43.0.7" if "get service control-plane" in " ".join(task.argv) else ""
        return TaskResult(task_id="", status="passed", return_code=0, stdout=stdout)


class Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)

    def status(self, label: str):  # pyright: ignore[reportMissingParameterType]
        del label
        return nullcontext()


def test_the_watcher_brackets_the_run() -> None:
    """Sampling has to start before the load and stop after it, whatever the
    load does — including raising."""
    watcher = FakeWatcher()

    _ = RunK6Task(run_k6=FakeRunK6(), watcher=watcher).run(TaskInputs.empty())

    assert watcher.events == ["start", "stop"]


def test_initial_replica_check_runs_before_load_sampling() -> None:
    events: list[str] = []

    class InitialCheck:
        def run(self) -> None:
            events.append("initial")

    class OrderedRun:
        def run(self, inputs: TaskInputs) -> TaskOutcome[K6RunResult]:
            del inputs
            events.append("k6")
            return TaskOutcome(value=_k6())

    @dataclass
    class OrderedWatcher:
        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    _ = RunK6Task(  # pyright: ignore[reportArgumentType]
        run_k6=OrderedRun(),
        watcher=OrderedWatcher(),
        initial_replicas=InitialCheck(),
    ).run(TaskInputs.empty())

    assert events == ["initial", "start", "k6", "stop"]


def test_the_watcher_stops_even_when_k6_blows_up() -> None:
    class Exploding:
        def run(self, inputs: TaskInputs) -> TaskOutcome[K6RunResult]:
            del inputs
            raise RuntimeError("k6 missing")

    watcher = FakeWatcher()

    with pytest.raises(RuntimeError, match="k6 missing"):
        _ = RunK6Task(run_k6=Exploding(), watcher=watcher).run(TaskInputs.empty())  # pyright: ignore[reportArgumentType]

    assert watcher.events == ["start", "stop"]


def test_a_run_without_autoscaling_needs_no_watcher() -> None:
    outcome = RunK6Task(run_k6=FakeRunK6()).run(TaskInputs.empty()).value

    assert outcome is not None
    assert outcome.autoscaling is None


def test_the_snapshot_covers_exactly_the_window_k6_ran_in() -> None:
    seen: list[TimeWindow] = []

    class FakeSnapshot:
        def run(self) -> Path:
            return Path("metrics/prometheus-snapshot.json")

    def snapshot(window: TimeWindow):  # pyright: ignore[reportMissingParameterType]
        seen.append(window)
        return FakeSnapshot()

    outcome = (
        CapturePrometheusTask(snapshot=snapshot)  # pyright: ignore[reportArgumentType]
        .run(_inputs(LoadtestOutcome(k6=_k6())))
        .value
    )

    assert seen == [TimeWindow(start=STARTED, end=ENDED)]
    assert outcome is not None
    assert outcome.prometheus_snapshot == Path("metrics/prometheus-snapshot.json")


def test_the_summary_receives_the_autoscaling_numbers_the_verifier_produced() -> None:
    numbers = AutoscalingSummary(
        deployment_name="fn-x", max_replicas_observed=4, final_desired_replicas=1
    )
    seen: list[AutoscalingSummary | None] = []

    class FakeSummary:
        def run(self) -> Path:
            return Path("summary.json")

    def summary(autoscaling: AutoscalingSummary | None):  # pyright: ignore[reportMissingParameterType]
        seen.append(autoscaling)
        return FakeSummary()

    _ = WriteSummaryTask(summary=summary).run(  # pyright: ignore[reportArgumentType]
        _inputs(LoadtestOutcome(k6=_k6(), autoscaling=numbers))
    )

    assert seen == [numbers]


def test_the_gate_fails_a_run_that_breached_its_thresholds() -> None:
    with pytest.raises(RuntimeError, match="thresholds failed"):
        _ = EvaluateGateTask().run(_inputs(LoadtestOutcome(k6=_k6(passed=False))))


def test_the_gate_passes_a_clean_run() -> None:
    outcome = EvaluateGateTask().run(_inputs(LoadtestOutcome(k6=_k6()))).value

    assert outcome is not None and outcome.k6.passed


@pytest.mark.parametrize(
    "task",
    [
        VerifyAutoscalingTask(verifier=None),  # pyright: ignore[reportArgumentType]
        CapturePrometheusTask(snapshot=lambda _window: None),  # pyright: ignore[reportArgumentType]
        WriteReportTask(report=None),  # pyright: ignore[reportArgumentType]
        WriteSummaryTask(summary=lambda _autoscaling: None),  # pyright: ignore[reportArgumentType]
        EvaluateGateTask(),
    ],
)
def test_no_step_can_see_a_run_that_did_not_happen(task: Any) -> None:
    """The whole reason this is one composite. In the legacy workflow these were
    separate tasks reading a mutable result attribute, so selecting one on its
    own failed for an unrelated ordering detail."""
    with pytest.raises(NoUpstreamValueError):
        _ = task.run(TaskInputs.empty())


def test_a_step_rejects_an_upstream_that_is_not_a_load_run() -> None:
    with pytest.raises(RuntimeError, match="expected the load run's outcome"):
        _ = EvaluateGateTask().run(_inputs("something else"))


def test_the_composite_is_one_unit_whose_steps_are_visible() -> None:
    executor = RecordingExecutor()
    composite = loadtest_composite(
        preflight=CommandTask(
            title="Check k6 is usable", argv=("k6", "version"), executor=executor, role="loadgen"
        ),
        prepare=CommandTask(
            title="Prepare the run directory",
            argv=("mkdir", "-p", "runs"),
            executor=executor,
            role="loadgen",
        ),
        run_k6=RunK6Task(run_k6=FakeRunK6()),
        steps_after_run=(EvaluateGateTask(),),
    )
    workflow = Workflow(workflow_id="loadtest")
    workflow.add(composite)

    sink = Sink()
    with bind_workflow_sink(sink):
        workflow.run()

    assert [task.task_id for task in workflow.compile().tasks] == ["001.run-the-load-test"]
    assert [
        event.task_id for event in sink.events if event.kind == "task.started"
    ] == [
        "001.run-the-load-test",
        "001.run-the-load-test/check-k6-is-usable",
        "001.run-the-load-test/prepare-the-run-directory",
        "001.run-the-load-test/run-k6",
        "001.run-the-load-test/evaluate-the-thresholds",
    ]


def test_a_breached_threshold_fails_the_whole_unit() -> None:
    executor = RecordingExecutor()
    composite = loadtest_composite(
        preflight=CommandTask(
            title="Check k6 is usable", argv=("k6", "version"), executor=executor, role="loadgen"
        ),
        prepare=CommandTask(
            title="Prepare the run directory", argv=("mkdir",), executor=executor, role="loadgen"
        ),
        run_k6=RunK6Task(run_k6=FakeRunK6(result_value=_k6(passed=False))),
        steps_after_run=(EvaluateGateTask(),),
    )
    workflow = Workflow(workflow_id="loadtest")
    workflow.add(composite)

    with pytest.raises(RuntimeError, match="thresholds failed"):
        workflow.run()


def _platform_request(**changes: Any) -> Any:
    from sonata_tasks.platform import PlatformFunction, PlatformRequest

    base: dict[str, Any] = {
        "backend": "k8s",
        "functions": (
            PlatformFunction(
                name="word-stats-java",
                image="localhost:5000/nanofaas/java-word-stats:e2e",
                payload="{}",
                build_argv=("./gradlew", ":functions:java:word-stats:bootBuildImage"),
            ),
        ),
    }
    return PlatformRequest(**{**base, **changes})


def _load(executor: RecordingExecutor) -> Any:
    from sonata_tasks.loadtest import loadtest_composite

    return loadtest_composite(
        preflight=CommandTask(
            title="Check k6 is usable", argv=("k6", "version"), executor=executor, role="loadgen"
        ),
        prepare=CommandTask(
            title="Prepare the run directory", argv=("mkdir",), executor=executor, role="loadgen"
        ),
        run_k6=RunK6Task(run_k6=FakeRunK6()),
        steps_after_run=(EvaluateGateTask(),),
    )


def test_loadtest_reuses_the_platform_validate_deploys() -> None:
    """The two workflows differ only in what they do once the platform is up, so
    the eight deployment units are the shared half rather than a second copy."""
    from sonata_engine import Workflow as SonataWorkflow
    from sonata_tasks.execution.bindings import RoleBindings

    from sonata_tasks.loadtest import build_loadtest_workflow

    executor = RecordingExecutor()
    workflow: SonataWorkflow = build_loadtest_workflow(
        _platform_request(),
        RoleBindings(host=executor, stack=executor, loadgen=executor),
        load=_load(executor),
    )

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.check-kubectl-is-usable",
        "002.build-control-plane",
        "003.build-image-localhost-5000-nanofaas-control-plane",
        "004.push-image-localhost-5000-nanofaas-control-plane",
        "005.build-image-word-stats-java",
        "006.push-image-localhost-5000-nanofaas-java-word-stats-e2e",
        "007.acquire-helm-release-nanofaas",
        "008.acquire-word-stats-java",
        "009.run-the-load-test",
        "010.release-word-stats-java",
        "011.release-helm-release-nanofaas",
    ]


def test_a_failed_load_test_still_deregisters_what_it_registered() -> None:
    from sonata_tasks.execution.bindings import RoleBindings

    from sonata_tasks.loadtest import build_loadtest_workflow, loadtest_composite

    executor = RecordingExecutor()
    breached = loadtest_composite(
        preflight=CommandTask(
            title="Check k6 is usable", argv=("k6", "version"), executor=executor, role="loadgen"
        ),
        prepare=CommandTask(
            title="Prepare the run directory", argv=("mkdir",), executor=executor, role="loadgen"
        ),
        run_k6=RunK6Task(run_k6=FakeRunK6(result_value=_k6(passed=False))),
        steps_after_run=(EvaluateGateTask(),),
    )
    workflow = build_loadtest_workflow(
        _platform_request(),
        RoleBindings(host=executor, stack=executor, loadgen=executor),
        load=breached,
    )

    with pytest.raises(RuntimeError, match="thresholds failed"):
        workflow.run()

    commands = [" ".join(spec.argv) for spec in executor.seen]
    assert any("-X DELETE" in command for command in commands)
    assert any("helm uninstall" in command for command in commands)
