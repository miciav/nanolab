from __future__ import annotations

from dataclasses import dataclass, field

from sonata_engine import TaskInputs
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.kubectl import KubectlTask


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
