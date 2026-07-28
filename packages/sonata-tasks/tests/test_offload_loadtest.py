from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sonata_engine import Steps, TaskInputs, Workflow
from sonata_engine.errors import NoUpstreamValueError
from workflow_tasks.execution.bindings import RoleBindings
from workflow_tasks.loadtest.models import K6RunResult
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.loadtest import LoadtestOutcome
from sonata_tasks.offload_loadtest import (
    EvaluateConservationTask,
    OffloadLoadtestRequest,
    build_offload_loadtest_workflow,
)
from sonata_tasks.platform import PlatformFunction, PlatformRequest


def _outcome() -> LoadtestOutcome:
    return LoadtestOutcome(
        k6=K6RunResult(
            summary_path=Path("k6-summary.json"),
            started_at=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
            ended_at=datetime(2026, 7, 28, 10, 5, tzinfo=UTC),
            passed=True,
        )
    )


def _inputs(upstream: object) -> TaskInputs:
    return replace(TaskInputs.empty(), _upstream=upstream)


@dataclass
class FakeEvaluator:
    error: Exception | None = None
    calls: list[str] = field(default_factory=list[str])

    def run(self) -> object:
        self.calls.append("run")
        if self.error is not None:
            raise self.error
        return {"passed": True}


def test_it_evaluates_once_the_load_has_run() -> None:
    evaluator = FakeEvaluator()

    outcome = EvaluateConservationTask(evaluate=evaluator).run(_inputs(_outcome())).value

    assert evaluator.calls == ["run"]
    assert outcome is not None and outcome.k6.passed


def test_a_divergence_fails_the_run_carrying_every_failure() -> None:
    """The evaluator collects them all rather than stopping at the first, which
    is the difference between "the numbers disagree" and knowing which ones."""
    evaluator = FakeEvaluator(
        error=RuntimeError(
            "offload conservation check failed: "
            "k6 requests for offloadable (100.0) diverges from "
            "edge function_success_total for offloadable (80.0) beyond tolerance 5; "
            "control function was offloaded"
        )
    )

    with pytest.raises(RuntimeError, match="diverges from.*beyond tolerance 5"):
        EvaluateConservationTask(evaluate=evaluator).run(_inputs(_outcome()))


def test_it_refuses_to_reconcile_a_run_that_did_not_happen() -> None:
    """Zero against zero reconciles perfectly and proves nothing."""
    evaluator = FakeEvaluator()

    with pytest.raises(NoUpstreamValueError):
        EvaluateConservationTask(evaluate=evaluator).run(TaskInputs.empty())

    assert evaluator.calls == []


def test_it_refuses_an_upstream_that_is_not_a_load_run() -> None:
    with pytest.raises(RuntimeError, match="expected the load run's outcome"):
        EvaluateConservationTask(evaluate=FakeEvaluator()).run(_inputs("something else"))


@dataclass
class ScriptedExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list[CommandTaskSpec])

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        stdout = "10.43.0.7" if "get service control-plane" in " ".join(task.argv) else ""
        return TaskResult(task_id="", status="passed", return_code=0, stdout=stdout)


OFFLOADABLE = PlatformFunction(
    name="word-stats-java", image="i:e2e", payload="{}", build_argv=("x",)
)
CONTROL = PlatformFunction(
    name="json-transform-java",
    image="j:e2e",
    payload="{}",
    build_argv=("y",),
    offload={"enabled": False},
)


def _request() -> OffloadLoadtestRequest:
    return OffloadLoadtestRequest(
        cloud=PlatformRequest(
            backend="k8s",
            # The cloud absorbs what the edge sheds, so it must not reproduce the
            # edge's own admission pressure.
            functions=(replace(OFFLOADABLE, concurrency=20, queue_size=100),),
            execution_role="cloud",
            label="cloud",
        ),
        edge=PlatformRequest(
            backend="k8s", functions=(OFFLOADABLE, CONTROL), label="edge"
        ),
    )


def _workflow(executor: ScriptedExecutor) -> Workflow:
    return build_offload_loadtest_workflow(
        _request(),
        RoleBindings(host=executor, stack=executor, cloud=executor),
        load=Steps(
            title="Run the offload load test",
            steps=(
                CommandTask(
                    title="Check k6 is usable",
                    argv=("k6", "version"),
                    executor=executor,
                    role="stack",
                ),
            ),
        ),
    )


def test_both_platforms_are_named_so_a_reader_can_tell_them_apart() -> None:
    """Without a label the plan shows "Check kubectl is usable" twice and says
    nothing about which cluster it means."""
    ids = [task.task_id for task in _workflow(ScriptedExecutor()).compile().tasks]

    assert ids[0] == "001.check-kubectl-is-usable-on-the-cloud"
    assert ids[6] == "007.check-kubectl-is-usable-on-the-edge"
    assert "015.acquire-helm-release-nanofaas-on-the-cloud" in ids
    assert "017.acquire-helm-release-nanofaas-on-the-edge" in ids


def test_the_cloud_comes_up_before_the_edge_that_offloads_to_it() -> None:
    ids = [task.task_id for task in _workflow(ScriptedExecutor()).compile().tasks]

    assert ids.index("015.acquire-helm-release-nanofaas-on-the-cloud") < ids.index(
        "017.acquire-helm-release-nanofaas-on-the-edge"
    )


def test_the_load_holds_every_registration_on_both_sides() -> None:
    """A failed load test still deregisters what it registered, on both
    clusters: the releases come after it, in reverse."""
    ids = [task.task_id for task in _workflow(ScriptedExecutor()).compile().tasks]

    assert ids[ids.index("020.run-the-offload-load-test") + 1 :] == [
        "021.release-json-transform-java-on-the-edge",
        "022.release-word-stats-java-on-the-edge",
        "023.release-helm-release-nanofaas-on-the-edge",
        "024.release-word-stats-java-on-the-cloud",
        "025.release-helm-release-nanofaas-on-the-cloud",
    ]


def test_the_control_function_registers_with_offload_disabled() -> None:
    """It is the control: if it were offloaded too, the conservation check could
    not tell offloaded traffic from ordinary traffic."""
    assert CONTROL.manifest().body()["offload"] == {"enabled": False}
    assert "offload" not in OFFLOADABLE.manifest().body()


def test_two_platforms_on_one_role_is_refused() -> None:
    """They are two clusters. Sharing a role would run both on one machine and
    the offload hop would not leave it."""
    same = PlatformRequest(backend="k8s", functions=(OFFLOADABLE,), label="cloud")

    with pytest.raises(ValueError, match="different roles"):
        _ = OffloadLoadtestRequest(cloud=same, edge=same)


def test_every_step_of_both_platforms_says_which_one_it_is() -> None:
    """The image build and push were the two that did not, so a plan showed
    "Build image …control-plane:e2e" twice with nothing to tell them apart —
    while the Gradle build right above it did say which side."""
    ids = [task.task_id for task in _workflow(ScriptedExecutor()).compile().tasks]
    platform_ids = [unit for unit in ids if "run-the-offload-load-test" not in unit]

    assert all(
        unit.endswith("-on-the-cloud") or unit.endswith("-on-the-edge")
        for unit in platform_ids
    ), [unit for unit in platform_ids if not unit.endswith(("-on-the-cloud", "-on-the-edge"))]
