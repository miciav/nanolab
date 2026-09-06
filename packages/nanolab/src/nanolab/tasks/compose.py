from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sonata_engine import Resource, Steps, TaskInputs
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.compose import (
    DeployDockerCompose,
    DestroyDockerCompose,
    DockerComposeProject as SharedDockerComposeProject,
    WaitForDockerCompose,
)
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor


@dataclass(frozen=True, slots=True)
class DockerComposeProject(SharedDockerComposeProject):
    role: str = "host"
    env: Mapping[str, str] = field(default_factory=dict)


def isolated_compose_resource(
    project: DockerComposeProject, *, executor: CommandTaskExecutor,
    cwd: Path | None = None, requires: tuple[Resource[Any], ...] = (),
) -> Resource[DockerComposeProject]:
    """Run the experiment in a fresh Compose project and remove all state."""
    options = CommandOptions(cwd=cwd, env=project.env)
    clear = DestroyDockerCompose(
        project, executor=executor, role=project.role, options=options,
        title=f"Clear any previous {project.name} state", remove_volumes=True,
        remove_orphans=True,
    )
    deploy = Steps(title=f"Acquire Docker Compose project {project.name}", steps=(
        clear,
        DeployDockerCompose(project, executor=executor, role=project.role, options=options),
        WaitForDockerCompose(project, executor=executor, role=project.role, options=options),
    ))
    destroy = DestroyDockerCompose(project, executor=executor, role=project.role,
                                   options=options, remove_volumes=True,
                                   remove_orphans=True)

    def acquire(inputs: TaskInputs) -> DockerComposeProject:
        _ = deploy.run(inputs)
        return project

    return compensated_resource(title=f"Acquire Docker Compose project {project.name}",
                                acquire=acquire, compensate=destroy.run,
                                requires=requires)


docker_compose_resource = isolated_compose_resource

__all__ = ["DeployDockerCompose", "DestroyDockerCompose", "DockerComposeProject",
           "WaitForDockerCompose", "docker_compose_resource", "isolated_compose_resource"]
