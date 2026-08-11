from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sonata_engine import Resource, Steps, TaskInputs
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole

from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import compensated_resource


@dataclass(frozen=True, slots=True)
class DockerComposeProject:
    """A Compose project built and deployed from one canonical file."""

    name: str
    file: Path
    ready_url: str
    role: ExecutionRole = "host"
    env: Mapping[str, str] = field(default_factory=dict)


class DeployDockerCompose(CommandTask):
    def __init__(
        self,
        project: DockerComposeProject,
        *,
        executor: CommandTaskExecutor,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Deploy Docker Compose project {project.name}",
            argv=(
                "docker",
                "compose",
                "-f",
                str(project.file),
                "-p",
                project.name,
                "up",
                "-d",
                "--build",
                "--wait",
            ),
            executor=executor,
            role=project.role,
            env=project.env,
            cwd=cwd,
        )


class WaitForDockerCompose(CommandTask):
    def __init__(
        self,
        project: DockerComposeProject,
        *,
        executor: CommandTaskExecutor,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Wait for Docker Compose project {project.name}",
            argv=(
                "curl",
                "-fsS",
                "--retry",
                "60",
                "--retry-delay",
                "1",
                "--retry-connrefused",
                "--retry-all-errors",
                project.ready_url,
            ),
            executor=executor,
            role=project.role,
            cwd=cwd,
        )


class DestroyDockerCompose(CommandTask):
    def __init__(
        self,
        project: DockerComposeProject,
        *,
        executor: CommandTaskExecutor,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Destroy Docker Compose project {project.name}",
            argv=(
                "docker",
                "compose",
                "-f",
                str(project.file),
                "-p",
                project.name,
                "down",
                "--remove-orphans",
            ),
            executor=executor,
            role=project.role,
            env=project.env,
            cwd=cwd,
        )


def docker_compose_resource(
    project: DockerComposeProject,
    *,
    executor: CommandTaskExecutor,
    cwd: Path | None = None,
    # Resource is invariant, so Resource[object] would reject the very resources
    # callers hold — a registry resource carries its own state type.
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[DockerComposeProject]:
    deploy = Steps(
        title=f"Acquire Docker Compose project {project.name}",
        steps=(
            DeployDockerCompose(project, executor=executor, cwd=cwd),
            WaitForDockerCompose(project, executor=executor, cwd=cwd),
        ),
    )
    destroy = DestroyDockerCompose(project, executor=executor, cwd=cwd)

    def acquire(inputs: TaskInputs) -> DockerComposeProject:
        _ = deploy.run(inputs)
        return project

    return compensated_resource(
        title=f"Acquire Docker Compose project {project.name}",
        acquire=acquire,
        compensate=destroy.run,
        requires=requires,
    )
