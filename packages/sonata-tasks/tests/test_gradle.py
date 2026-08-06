from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.gradle import GradleTask


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout="")


def test_it_runs_the_wrapper_and_never_leaves_a_daemon_behind() -> None:
    executor = RecordingExecutor()

    task = GradleTask(":control-plane:bootJar", executor=executor, role="stack")
    _ = task.run(TaskInputs.empty())

    assert task.title == "Run gradle :control-plane:bootJar"
    assert executor.seen[0].argv == ("./gradlew", ":control-plane:bootJar", "--no-daemon")
    assert executor.seen[0].execution_role == "stack"


def test_it_assembles_the_property_flags_callers_were_writing_by_hand() -> None:
    executor = RecordingExecutor()

    _ = GradleTask(
        ":control-plane:bootJar",
        executor=executor,
        role="host",
        properties={"controlPlaneModules": "k8s-deployment-provider"},
    ).run(TaskInputs.empty())

    assert executor.seen[0].argv == (
        "./gradlew",
        ":control-plane:bootJar",
        "-PcontrolPlaneModules=k8s-deployment-provider",
        "--no-daemon",
    )


def test_properties_come_after_the_targets_so_gradle_reads_them_as_flags() -> None:
    executor = RecordingExecutor()

    _ = GradleTask(
        ":a:jar",
        ":b:jar",
        executor=executor,
        role="host",
        properties={"one": "1", "two": "2"},
    ).run(TaskInputs.empty())

    assert executor.seen[0].argv == (
        "./gradlew",
        ":a:jar",
        ":b:jar",
        "-Pone=1",
        "-Ptwo=2",
        "--no-daemon",
    )


def test_a_caller_can_name_the_step_something_a_reader_recognises() -> None:
    task = GradleTask(
        ":control-plane:bootJar",
        executor=RecordingExecutor(),
        role="host",
        title="Build control plane jar",
    )

    assert task.title == "Build control plane jar"


def test_it_refuses_to_run_gradle_with_nothing_to_build() -> None:
    with pytest.raises(ValueError, match="at least one target"):
        _ = GradleTask(executor=RecordingExecutor(), role="host")


def test_it_is_a_command_task_rather_than_a_wrapper_around_one() -> None:
    assert isinstance(
        GradleTask(":a:jar", executor=RecordingExecutor(), role="host"), CommandTask
    )
