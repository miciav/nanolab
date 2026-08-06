from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sonata_engine import Workflow
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.compose import DockerComposeProject, docker_compose_resource


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0)


@dataclass
class FailingReadinessExecutor(RecordingExecutor):
    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        if task.argv[0] == "curl":
            return TaskResult(
                task_id="",
                status="failed",
                return_code=7,
                stderr="not ready",
            )
        return TaskResult(task_id="", status="passed", return_code=0)


def test_compose_resource_builds_deploys_and_tears_down_the_project() -> None:
    executor = RecordingExecutor()
    project = DockerComposeProject(
        name="nanofaas",
        file=Path("deploy/compose/compose.yaml"),
        ready_url="http://127.0.0.1:8081/actuator/health/readiness",
    )
    resource = docker_compose_resource(project, executor=executor, cwd=Path("/nanofaas"))
    workflow = Workflow("compose")
    workflow.add(
        CommandTask(title="Use deployment", argv=("true",), executor=executor),
        requires=(resource,),
    )

    workflow.run()

    assert [task.argv for task in executor.seen] == [
        (
            "docker",
            "compose",
            "-f",
            "deploy/compose/compose.yaml",
            "-p",
            "nanofaas",
            "up",
            "-d",
            "--build",
            "--wait",
        ),
        (
            "curl",
            "-fsS",
            "--retry",
            "60",
            "--retry-delay",
            "1",
            "--retry-connrefused",
            "--retry-all-errors",
            "http://127.0.0.1:8081/actuator/health/readiness",
        ),
        ("true",),
        (
            "docker",
            "compose",
            "-f",
            "deploy/compose/compose.yaml",
            "-p",
            "nanofaas",
            "down",
            "--remove-orphans",
        ),
    ]
    assert executor.seen[0].cwd == Path("/nanofaas")
    assert executor.seen[-1].cwd == Path("/nanofaas")


def test_compose_resource_passes_project_environment_to_compose() -> None:
    executor = RecordingExecutor()
    project = DockerComposeProject(
        name="nanofaas-loadtest",
        file=Path("compose.yaml"),
        ready_url="http://127.0.0.1:8081/actuator/health/readiness",
        env={
            "NANOFAAS_CONTROL_PLANE_MODULES": (
                "container-deployment-provider,autoscaler,async-queue,sync-queue"
            )
        },
    )
    workflow = Workflow("compose")
    workflow.add(
        CommandTask(title="Use deployment", argv=("true",), executor=executor),
        requires=(docker_compose_resource(project, executor=executor),),
    )

    workflow.run()

    assert executor.seen[0].env == project.env


def test_compose_resource_is_named_as_one_lifecycle_in_the_plan() -> None:
    executor = RecordingExecutor()
    resource = docker_compose_resource(
        DockerComposeProject(
            name="nanofaas",
            file=Path("compose.yaml"),
            ready_url="http://127.0.0.1:8081/actuator/health/readiness",
        ),
        executor=executor,
    )
    workflow = Workflow("compose")
    workflow.add(
        CommandTask(title="Use deployment", argv=("true",), executor=executor),
        requires=(resource,),
    )

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.acquire-docker-compose-project-nanofaas",
        "002.use-deployment",
        "003.release-docker-compose-project-nanofaas",
    ]


def test_compose_resource_tears_down_when_readiness_fails() -> None:
    executor = FailingReadinessExecutor()
    resource = docker_compose_resource(
        DockerComposeProject(
            name="nanofaas-validate",
            file=Path("compose.yaml"),
            ready_url="http://127.0.0.1:8081/actuator/health/readiness",
        ),
        executor=executor,
    )
    workflow = Workflow("compose")
    workflow.add(
        CommandTask(title="Use deployment", argv=("true",), executor=executor),
        requires=(resource,),
    )

    with pytest.raises(RuntimeError, match="not ready"):
        workflow.run()

    assert executor.seen[-1].argv == (
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-p",
        "nanofaas-validate",
        "down",
        "--remove-orphans",
    )
