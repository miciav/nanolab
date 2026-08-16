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
    # False when the image was built elsewhere and must be used as it is. The
    # compose service declares both `image:` and `build:`, so `up --build`
    # rebuilds from the Dockerfile and tags the result with the image name —
    # which silently overwrites a natively-compiled image with a JVM one.
    build: bool = True


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
                *(("--build",) if project.build else ()),
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
    """Tears the project down, volumes included.

    `--volumes` is not tidiness. Without it the Prometheus volume outlived the
    project, so a later run's query window opened on the previous run's series:
    the control plane restarted and its counters went back to zero mid-window,
    which read as a negative delta and quietly understated everything derived
    from it. Every result worth keeping is already written to the run directory
    before teardown, so nothing is lost by starting each run from empty.
    """

    def __init__(
        self,
        project: DockerComposeProject,
        *,
        executor: CommandTaskExecutor,
        cwd: Path | None = None,
        title: str | None = None,
    ) -> None:
        super().__init__(
            title=title or f"Destroy Docker Compose project {project.name}",
            argv=(
                "docker",
                "compose",
                "-f",
                str(project.file),
                "-p",
                project.name,
                "down",
                "--volumes",
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
            # Deploying onto whatever the last run left is how a crashed run
            # contaminates the next one: compensation only runs when the run gets
            # far enough to have something to compensate. Starting from empty
            # costs a few seconds and makes each run's state its own.
            DestroyDockerCompose(
                project,
                executor=executor,
                cwd=cwd,
                title=f"Clear any previous {project.name} state",
            ),
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
