from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from sonata_engine.errors import NoUpstreamValueError
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.kubectl import (
    ClusterIpEndpointTask,
    KubectlTask,
    k8s_function_resources_absent,
)


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0)


def test_the_namespace_lands_before_the_subcommand() -> None:
    executor = RecordingExecutor()

    _ = KubectlTask(
        "get", "deployment", "fn-1", executor=executor, role="stack", namespace="nf"
    ).run(TaskInputs.empty())

    assert executor.seen[0].argv == ("kubectl", "-n", "nf", "get", "deployment", "fn-1")


def test_commands_without_a_namespace_get_none() -> None:
    """`version --client` talks to no cluster, so a namespace would be wrong."""
    executor = RecordingExecutor()

    _ = KubectlTask("version", "--client", executor=executor, role="stack").run(
        TaskInputs.empty()
    )

    assert executor.seen[0].argv == ("kubectl", "version", "--client")


def test_the_title_defaults_to_the_command_and_can_be_overridden() -> None:
    executor = RecordingExecutor()

    assert (
        KubectlTask("version", "--client", executor=executor, role="stack").title
        == "kubectl version --client"
    )
    assert (
        KubectlTask(
            "get", "pods", executor=executor, role="stack", title="List pods"
        ).title
        == "List pods"
    )


def _with_upstream(value: object) -> TaskInputs:
    from dataclasses import replace

    return replace(TaskInputs.empty(), _upstream=value)


def _result(stdout: str) -> TaskResult:
    return TaskResult(task_id="", status="passed", return_code=0, stdout=stdout)


def test_it_builds_the_url_from_the_address_the_previous_step_read() -> None:
    task = ClusterIpEndpointTask(service="control-plane", port=8080)

    outcome = task.run(_with_upstream(_result(" 10.43.0.7 \n")))

    assert outcome.value == "http://10.43.0.7:8080"
    assert task.title == "Resolve where control-plane answers"


def test_a_service_with_no_address_fails_rather_than_yielding_a_broken_url() -> None:
    """`http://:8080` would be accepted by curl and fail much later, somewhere
    that says nothing about the Service."""
    task = ClusterIpEndpointTask(service="control-plane", port=8080)

    with pytest.raises(RuntimeError, match="no ClusterIP"):
        _ = task.run(_with_upstream(_result("   ")))


def test_it_refuses_an_upstream_that_is_not_a_command_result() -> None:
    task = ClusterIpEndpointTask(service="control-plane", port=8080)

    with pytest.raises(RuntimeError, match="expected the previous step's command result"):
        _ = task.run(_with_upstream("10.43.0.7"))


def test_it_says_so_when_nothing_ran_before_it() -> None:
    """Outside a composite there is no upstream at all — a different mistake
    from an empty address, and the engine names it."""
    task = ClusterIpEndpointTask(service="control-plane", port=8080)

    with pytest.raises(NoUpstreamValueError):
        _ = task.run(TaskInputs.empty())


def test_it_waits_until_a_deleted_function_has_no_deployment_or_service() -> None:
    executor = RecordingExecutor()

    _ = k8s_function_resources_absent(
        function="word-stats",
        namespace="nf",
        executor=executor,
        role="stack",
    ).run(TaskInputs.empty())

    script = executor.seen[0].argv[-1]
    assert executor.seen[0].argv[:2] == ("bash", "-lc")
    assert "deployment/fn-word-stats" in script
    assert "service/fn-word-stats" in script
    assert "kubectl -n nf get" in script
