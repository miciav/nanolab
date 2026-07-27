from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.docker import DockerBuildTask, DockerInspectTask, DockerPushTask


@dataclass
class RecordingExecutor:
    stdout: str = ""
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=self.stdout)


def test_build_names_the_image_and_orders_the_flags() -> None:
    executor = RecordingExecutor()

    task = DockerBuildTask(
        image="reg/app:e2e",
        dockerfile="platform/control-plane/Dockerfile",
        context="platform/control-plane",
        executor=executor,
        role="host",
    )
    _ = task.run(TaskInputs.empty())

    assert task.title == "Build image reg/app:e2e"
    assert executor.seen[0].argv == (
        "docker",
        "build",
        "-f",
        "platform/control-plane/Dockerfile",
        "-t",
        "reg/app:e2e",
        "platform/control-plane",
    )


def test_push_sends_the_image_its_tag_names() -> None:
    executor = RecordingExecutor()

    task = DockerPushTask(image="reg/app:e2e", executor=executor, role="stack")
    _ = task.run(TaskInputs.empty())

    assert task.title == "Push image reg/app:e2e"
    assert executor.seen[0].argv == ("docker", "push", "reg/app:e2e")
    assert executor.seen[0].execution_role == "stack"


def test_inspect_reads_the_whole_host_config_as_json_by_default() -> None:
    executor = RecordingExecutor(stdout="{}")

    _ = DockerInspectTask(container="fn-1", executor=executor, role="host").run(TaskInputs.empty())

    assert executor.seen[0].argv == (
        "docker",
        "inspect",
        "--format={{json .HostConfig}}",
        "fn-1",
    )


def test_inspect_accepts_a_narrower_template() -> None:
    executor = RecordingExecutor(stdout="running")

    _ = DockerInspectTask(
        container="fn-1", executor=executor, role="host", fmt="{{.State.Status}}"
    ).run(TaskInputs.empty())

    assert executor.seen[0].argv[2] == "--format={{.State.Status}}"


def test_inspect_carries_a_verify_hook() -> None:
    executor = RecordingExecutor(stdout="{}")

    def reject(_result: TaskResult) -> None:
        raise RuntimeError("unusable")

    task = DockerInspectTask(container="fn-1", executor=executor, role="host", verify=reject)

    with pytest.raises(RuntimeError, match="unusable"):
        task.run(TaskInputs.empty())


@pytest.mark.parametrize(
    "task",
    [
        DockerBuildTask(
            image="i", dockerfile="d", context="c", executor=RecordingExecutor(), role="host"
        ),
        DockerPushTask(image="i", executor=RecordingExecutor(), role="host"),
        DockerInspectTask(container="c", executor=RecordingExecutor(), role="host"),
    ],
)
def test_they_are_command_tasks_rather_than_wrappers_around_one(task: CommandTask) -> None:
    """Subclassing keeps role, cwd, exit codes and verify working without a second
    layer to maintain — and lets a workflow treat them as ordinary tasks."""
    assert isinstance(task, CommandTask)
