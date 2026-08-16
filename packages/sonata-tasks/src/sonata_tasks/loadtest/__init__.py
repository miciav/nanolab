from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, override

from sonata_engine import Resource, Steps, Task, TaskInputs, TaskOutcome, Workflow
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor
from sonata_tasks.loadtest.autoscaling import (
    AutoscalingSummary,
    InitialReplicaCheck,
    Sampling,
    VerifyAutoscalingReplicas,
)
from sonata_tasks.loadtest.concurrency import (
    ConcurrencySummary,
    ConcurrencyWatcher,
    ConcurrencyWatcherGroup,
    verify_concurrency_cycle,
    verify_observable,
    write_series,
)
from sonata_tasks.loadtest.models import K6RunResult, TimeWindow
from sonata_tasks.loadtest.tasks import (
    CapturePrometheusSnapshot,
    FetchVmResults,
    WriteK6Report,
    WriteLoadtestSummary,
)

from sonata_tasks.command import CommandTask
from sonata_tasks.deployment import LOCAL_CONTROL_PLANE_API_PORT
from sonata_tasks.platform import PlatformRequest, add_platform

# Sonata steps over the load-test implementations in the sibling submodules
# (`.tasks`, `.autoscaling`, `.models`), which are ordinary classes with a
# `run()` and no dependency on the legacy engine — so they are reused rather
# than rewritten. What changes is how they find each other's results.


@dataclass(frozen=True, slots=True)
class LoadtestOutcome:
    """What the load run produced, accumulated as the steps go.

    The legacy tasks read each other's results off attributes: the Prometheus
    snapshot took `lambda: run_k6.result.started_at`, the summary took the
    verifier object, while the gate consumed a mutable result attribute. That
    temporal coupling made an out-of-order step fail obscurely.

    Here it travels forward through `inputs.upstream()` instead, so a step can
    only see what genuinely ran before it.
    """

    k6: K6RunResult
    autoscaling: AutoscalingSummary | None = None
    concurrency: ConcurrencySummary | None = None
    prometheus_snapshot: Path | None = None
    report: Path | None = None
    summary: Path | None = None


def load_outcome(inputs: TaskInputs, title: str) -> LoadtestOutcome:
    """The load run's outcome from the preceding step, or a legible refusal.

    Public because `offload_loadtest` reconciles the same run from its own
    module: a step that needs what the load produced should not have to reach
    for a private helper to say so.
    """
    value = inputs.upstream()
    if not isinstance(value, LoadtestOutcome):
        raise RuntimeError(
            f"{title}: expected the load run's outcome, got {type(value).__name__}"
        )
    return value


class RunK6Task(Task[LoadtestOutcome]):
    """Run k6, optionally sampling replica counts while it runs.

    The watcher wraps the run rather than sitting beside it: sampling has to
    start before the load and stop after it, whatever the load does.
    """

    def __init__(
        self,
        *,
        run_k6: Task[K6RunResult],
        # Sampling, not Watcher: this only brackets the run. Reading the peak is
        # the verifier's job, and asking for it here would exclude a sampler
        # that reports somewhere else.
        watcher: Sampling | None = None,
        initial_replicas: InitialReplicaCheck | None = None,
        title: str = "Run k6",
    ) -> None:
        self.title = title
        self._run_k6 = run_k6
        self._watcher = watcher
        self._initial_replicas: InitialReplicaCheck | None = initial_replicas

    def _run(self) -> K6RunResult:
        # K6Task always carries its result; the outcome type allows None, and an
        # unchecked None would surface later as an attribute error on the
        # summary rather than as a load test that produced nothing.
        outcome = self._run_k6.run(TaskInputs.empty())
        if outcome.value is None:
            raise RuntimeError(f"{self.title}: k6 produced no result")
        return outcome.value

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[LoadtestOutcome]:
        del inputs
        if self._initial_replicas is not None:
            self._initial_replicas.run()
        if self._watcher is None:
            return TaskOutcome(value=LoadtestOutcome(k6=self._run()))
        self._watcher.start()
        try:
            result = self._run()
        finally:
            self._watcher.stop()
        return TaskOutcome(value=LoadtestOutcome(k6=result))


class VerifyAutoscalingTask(Task[LoadtestOutcome]):
    """Assert the replica count rose under load and fell after it."""

    def __init__(
        self,
        *,
        verifier: VerifyAutoscalingReplicas,
        title: str = "Verify autoscaling replicas",
    ) -> None:
        self.title = title
        self._verifier = verifier

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[LoadtestOutcome]:
        outcome = load_outcome(inputs, self.title)
        return TaskOutcome(value=replace(outcome, autoscaling=self._verifier.run()))


class VerifyConcurrencyTask(Task[LoadtestOutcome]):
    """Assert the governor lowered the per-replica limit under load and raised it after.

    The counterpart of VerifyAutoscalingTask for runs that hold the replica count
    still. It has nothing to poll after the fact: the excursion only exists in
    the series the watcher took while k6 was running, because the governor
    restores the limit once the function speeds back up.
    """

    def __init__(
        self,
        *,
        watcher: ConcurrencyWatcher,
        function_name: str,
        series_path: Path | None = None,
        title: str = "Verify concurrency governor",
    ) -> None:
        self.title = title
        self._watcher = watcher
        self._function_name = function_name
        self._series_path = series_path

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[LoadtestOutcome]:
        outcome = load_outcome(inputs, self.title)
        summary = self._watcher.summary(self._function_name)
        # Written before the verdict, on purpose: the readings are most needed
        # by the run that fails, and a raise here would take them with it.
        if self._series_path is not None:
            write_series(summary, self._series_path)
        print(summary.describe())
        verify_concurrency_cycle(summary)
        return TaskOutcome(value=replace(outcome, concurrency=summary))


class ReportCoTenancyTask(Task[LoadtestOutcome]):
    """Record what each governor did while sharing a control plane with the other.

    Reports rather than judges. The run exists to find out whether one function's
    load moves its neighbour's limit, and asserting that before measuring it
    would be assuming the answer. What it does assert is that the run could have
    answered: both functions produced readings, and both were seen idle and
    saturated. A silent run and a run that disproved the effect must not look
    alike.
    """

    def __init__(
        self,
        *,
        watchers: ConcurrencyWatcherGroup,
        series_dir: Path | None = None,
        title: str = "Report co-tenancy",
    ) -> None:
        self.title = title
        self._watchers = watchers
        self._series_dir = series_dir

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[LoadtestOutcome]:
        outcome = load_outcome(inputs, self.title)
        summaries = self._watchers.summaries()
        for name, summary in summaries.items():
            if self._series_dir is not None:
                write_series(summary, self._series_dir / f"concurrency-series-{name}.json")
            print(summary.describe())
        for summary in summaries.values():
            verify_observable(summary)
        # The neighbour's series is the point, so the outcome keeps the function
        # the run was built around and the files keep the rest.
        first = next(iter(summaries.values()), None)
        return TaskOutcome(value=replace(outcome, concurrency=first))


class CapturePrometheusTask(Task[LoadtestOutcome]):
    """Snapshot the metrics for exactly the window k6 ran in.

    The window comes from the upstream outcome, so it cannot be captured for a
    run that did not happen — the legacy version resolved it through a lambda
    closing over a task whose `result` might not exist yet.
    """

    def __init__(
        self,
        *,
        snapshot: Callable[[TimeWindow], CapturePrometheusSnapshot],
        title: str = "Capture Prometheus snapshot",
    ) -> None:
        self.title = title
        self._snapshot = snapshot

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[LoadtestOutcome]:
        outcome = load_outcome(inputs, self.title)
        window = TimeWindow(start=outcome.k6.started_at, end=outcome.k6.ended_at)
        return TaskOutcome(
            value=replace(outcome, prometheus_snapshot=self._snapshot(window).run())
        )


class WriteReportTask(Task[LoadtestOutcome]):
    """Render the HTML report from what the run left on disk."""

    def __init__(self, *, report: WriteK6Report, title: str = "Write the report") -> None:
        self.title = title
        self._report = report

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[LoadtestOutcome]:
        outcome = load_outcome(inputs, self.title)
        return TaskOutcome(value=replace(outcome, report=self._report.run()))


class WriteSummaryTask(Task[LoadtestOutcome]):
    """Write summary.json, including the autoscaling numbers when there are any."""

    def __init__(
        self,
        *,
        summary: Callable[[AutoscalingSummary | None], WriteLoadtestSummary],
        title: str = "Write the summary",
    ) -> None:
        self.title = title
        self._summary = summary

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[LoadtestOutcome]:
        outcome = load_outcome(inputs, self.title)
        written = self._summary(outcome.autoscaling).run()
        return TaskOutcome(value=replace(outcome, summary=written))


class EvaluateGateTask(Task[LoadtestOutcome]):
    """Fail the run when k6 breached its thresholds or the autoscaler oscillated.

    Last on purpose: k6 exits 99 on a breach having still written its summary,
    so the report and the snapshot are produced before this decides. The
    autoscaling verdict is made here for the same reason — raising it inside the
    verifier would destroy the trajectory that explains it.
    """

    def __init__(self, *, title: str = "Evaluate the thresholds") -> None:
        self.title = title

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[LoadtestOutcome]:
        outcome = load_outcome(inputs, self.title)
        autoscaling = outcome.autoscaling
        if autoscaling is not None and autoscaling.releases_under_load > 0:
            # Client-side thresholds cannot see this: the platform retries the
            # requests that were in flight when the replicas went away, so k6
            # reports success while the function was dropped and re-fetched
            # mid-load.
            raise RuntimeError(
                f"the autoscaler released {autoscaling.deployment_name} "
                f"{autoscaling.releases_under_load} time(s) while traffic was "
                "still flowing, dropping it to zero and fetching it back. See "
                "replica_samples in summary.json"
            )
        if not outcome.k6.passed:
            raise RuntimeError("k6 thresholds failed; see report.html")
        return TaskOutcome(value=outcome)


class FetchResultsTask(Task[LoadtestOutcome]):
    """Bring a remote loadgen's summary back to the run directory."""

    def __init__(self, *, fetch: FetchVmResults, title: str = "Fetch k6 results") -> None:
        self.title = title
        self._fetch = fetch

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[LoadtestOutcome]:
        outcome = load_outcome(inputs, self.title)
        _ = self._fetch.run()
        return TaskOutcome(value=outcome)


def loadtest_composite(
    *,
    preflight: Task[Any],
    prepare: CommandTask,
    run_k6: RunK6Task,
    steps_after_run: tuple[Task[Any], ...],
    title: str = "Run the load test",
) -> Steps:
    """The whole load test as one compiled unit.

    One unit because the steps are not independently runnable: every one after
    the run needs what the run produced. The legacy workflow made them separate
    tasks that reached into each other's attributes. Here the steps show up in
    the event stream instead, and
    `--only` names the load test, which is the thing you can actually re-run.
    """
    return Steps(title=title, steps=(preflight, prepare, run_k6, *steps_after_run))


def build_loadtest_workflow(
    request: PlatformRequest,
    bindings: RoleBindings,
    *,
    load: Steps,
    workflow_id: str = "loadtest",
    cwd: Path | None = None,
    requires: tuple[Resource[Any], ...] = (),
    local_endpoint: str = f"http://127.0.0.1:{LOCAL_CONTROL_PLANE_API_PORT}",
) -> Workflow:
    """Deploy the platform, register the functions, then put them under load.

    The platform half is `add_platform`, shared with `validate`: the two differ
    only in what they do once it is up. The load half arrives already assembled,
    because building it needs a k6 runner, a Prometheus client and a run
    directory — none of which this package should know how to obtain.

    The load composite declares every function resource, so the compiler
    releases them after it rather than before, and a failed load test still
    deregisters what it registered.
    """
    executor = RoleBoundCommandTaskExecutor(bindings)
    workflow = Workflow(workflow_id=workflow_id)
    platform = add_platform(
        workflow,
        request,
        executor=executor,
        cwd=cwd,
        local_endpoint=local_endpoint,
        requires=requires,
    )
    workflow.add(load, requires=(*requires, *platform.resources, *platform.functions))
    return workflow
