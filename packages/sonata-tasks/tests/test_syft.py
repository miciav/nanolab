from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.syft import SYFT_IMAGE, SyftTask


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


class TestSyftTask:
    def test_argv_builds_correct_docker_run_command(self) -> None:
        executor = RecordingExecutor()
        task = SyftTask(
            image="reg/app:1.0.0",
            output_path="/tmp/sboms/app.spdx.json",
            docker_config="/home/user/.docker",
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())

        assert task.title == "Syft SBOM reg/app:1.0.0"
        assert executor.seen[0].argv == (
            "docker",
            "run",
            "--rm",
            "--env",
            "DOCKER_CONFIG=/auth",
            "--volume",
            "/home/user/.docker:/auth:ro",
            "--volume",
            "/tmp/sboms:/out",
            SYFT_IMAGE,
            "reg/app:1.0.0",
            "-o",
            "spdx-json=/out/app.spdx.json",
        )

    def test_different_output_path(self) -> None:
        executor = RecordingExecutor()
        task = SyftTask(
            image="reg/app:latest",
            output_path="/tmp/out/report.spdx.json",
            docker_config="/cfg/docker",
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())

        expected_argv = (
            "docker",
            "run",
            "--rm",
            "--env",
            "DOCKER_CONFIG=/auth",
            "--volume",
            "/cfg/docker:/auth:ro",
            "--volume",
            "/tmp/out:/out",
            SYFT_IMAGE,
            "reg/app:latest",
            "-o",
            "spdx-json=/out/report.spdx.json",
        )
        assert executor.seen[0].argv == expected_argv

    def test_fails_on_error(self) -> None:
        executor = FailingExecutor()
        task = SyftTask(
            image="reg/app:1.0.0",
            output_path="/tmp/sboms/app.spdx.json",
            docker_config="/auth",
            executor=executor,
            role="host",
        )
        with pytest.raises(RuntimeError, match="unauthorized"):
            task.run(TaskInputs.empty())


class TestSyftTaskHierarchy:
    def test_is_a_command_task(self) -> None:
        executor = RecordingExecutor()
        assert isinstance(
            SyftTask(
                image="i",
                output_path="/tmp/o.spdx.json",
                docker_config="d",
                executor=executor,
                role="host",
            ),
            CommandTask,
        )
