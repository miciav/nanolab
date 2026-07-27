from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask

# Every task here takes `role` without a default. Building an image on the host
# and building it inside a VM are different daemons, different caches, and the
# result lands somewhere else — a default would hide the one decision the reader
# of a call site most needs to see.


class DockerBuildTask(CommandTask):
    """Build an image from a Dockerfile."""

    def __init__(
        self,
        *,
        image: str,
        dockerfile: str,
        context: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Build image {image}",
            argv=("docker", "build", "-f", dockerfile, "-t", image, context),
            executor=executor,
            role=role,
            cwd=cwd,
        )


class DockerPushTask(CommandTask):
    """Push an image to the registry its tag names."""

    def __init__(
        self,
        *,
        image: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Push image {image}",
            argv=("docker", "push", image),
            executor=executor,
            role=role,
            cwd=cwd,
        )


class DockerInspectTask(CommandTask):
    """Read a container's metadata.

    `fmt` is a Go template, defaulting to the whole host config as JSON so a
    `verify` hook can parse it. Callers that want a specific field pass their own.
    """

    def __init__(
        self,
        *,
        container: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        fmt: str = "{{json .HostConfig}}",
        cwd: Path | None = None,
        verify: Callable[[TaskResult], None] | None = None,
    ) -> None:
        super().__init__(
            title=f"Inspect {container}",
            argv=("docker", "inspect", f"--format={fmt}", container),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify,
        )
