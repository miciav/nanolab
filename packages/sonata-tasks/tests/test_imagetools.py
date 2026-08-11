from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.imagetools import ImagetoolsCreateTask, ImagetoolsInspectTask


@dataclass
class RecordingExecutor:
    stdout: str = ""
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=self.stdout)


@dataclass
class FailingExecutor:
    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        return TaskResult(
            task_id="",
            status="failed",
            return_code=1,
            stderr="unauthorized: authentication required",
        )


class TestImagetoolsCreateTask:
    def test_argv_includes_tag_and_sources(self) -> None:
        executor = RecordingExecutor()
        task = ImagetoolsCreateTask(
            tag="reg/app:manifest",
            sources=("reg/app:1.0.0", "reg/app:2.0.0"),
            docker_config="/home/user/.docker",
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())

        assert task.title == "Create manifest reg/app:manifest"
        assert executor.seen[0].argv == (
            "docker", "buildx", "imagetools", "create",
            "--tag", "reg/app:manifest",
            "reg/app:1.0.0", "reg/app:2.0.0",
        )

    def test_sets_docker_config_env(self) -> None:
        executor = RecordingExecutor()
        task = ImagetoolsCreateTask(
            tag="reg/app:manifest",
            sources=("reg/src:tag",),
            docker_config="/custom/.docker",
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())

        assert executor.seen[0].env == {"DOCKER_CONFIG": "/custom/.docker"}

    def test_single_source(self) -> None:
        executor = RecordingExecutor()
        task = ImagetoolsCreateTask(
            tag="reg/app:manifest",
            sources=("reg/app:1.0.0",),
            docker_config="/.docker",
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())

        assert executor.seen[0].argv == (
            "docker", "buildx", "imagetools", "create",
            "--tag", "reg/app:manifest",
            "reg/app:1.0.0",
        )

    def test_fails_on_error(self) -> None:
        executor = FailingExecutor()
        task = ImagetoolsCreateTask(
            tag="reg/app:manifest",
            sources=("reg/src:tag",),
            docker_config="/auth.json",
            executor=executor,
            role="host",
        )
        with pytest.raises(RuntimeError, match="unauthorized"):
            task.run(TaskInputs.empty())


class TestImagetoolsInspectTask:
    def test_argv_contains_the_reference(self) -> None:
        executor = RecordingExecutor(stdout="sha256:abc123")
        task = ImagetoolsInspectTask(
            reference="reg/app:manifest",
            docker_config="/home/user/.docker",
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())

        assert task.title == "Inspect manifest reg/app:manifest"
        assert executor.seen[0].argv == (
            "docker", "buildx", "imagetools", "inspect",
            "reg/app:manifest",
        )

    def test_sets_docker_config_env(self) -> None:
        executor = RecordingExecutor(stdout="sha256:abc123")
        task = ImagetoolsInspectTask(
            reference="reg/app:manifest",
            docker_config="/custom/.docker",
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())

        assert executor.seen[0].env == {"DOCKER_CONFIG": "/custom/.docker"}

    def test_returns_stdout(self) -> None:
        executor = RecordingExecutor(stdout="sha256:def456")
        task = ImagetoolsInspectTask(
            reference="reg/app:manifest",
            docker_config="/.docker",
            executor=executor,
            role="host",
        )
        result = task.run(TaskInputs.empty())

        assert result.value is not None
        assert result.value.stdout == "sha256:def456"

    def test_fails_on_error(self) -> None:
        executor = FailingExecutor()
        task = ImagetoolsInspectTask(
            reference="reg/app:manifest",
            docker_config="/auth.json",
            executor=executor,
            role="host",
        )
        with pytest.raises(RuntimeError, match="unauthorized"):
            task.run(TaskInputs.empty())


class TestImagetoolsTaskHierarchy:
    def test_they_are_command_tasks(self) -> None:
        executor = RecordingExecutor()
        assert isinstance(
            ImagetoolsCreateTask(
                tag="t",
                sources=("s",),
                docker_config="d",
                executor=executor,
                role="host",
            ),
            CommandTask,
        )
        assert isinstance(
            ImagetoolsInspectTask(
                reference="r",
                docker_config="d",
                executor=executor,
                role="host",
            ),
            CommandTask,
        )
