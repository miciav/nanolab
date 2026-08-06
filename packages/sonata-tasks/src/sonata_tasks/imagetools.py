from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import TaskResult

from sonata_tasks.docker import DockerTask


class ImagetoolsCreateTask(DockerTask):
    """Create a multi-architecture manifest with ``docker buildx imagetools create``.

    The ``docker_config`` directory is made available to the subprocess via the
    ``DOCKER_CONFIG`` environment variable so it can read registry credentials
    from the right location.
    """

    def __init__(
        self,
        *,
        tag: str,
        sources: tuple[str, ...],
        docker_config: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        title: str | None = None,
        cwd: Path | None = None,
        verify: Callable[[TaskResult], None] | None = None,
    ) -> None:
        super().__init__(
            "buildx", "imagetools", "create", "--tag", tag, *sources,
            executor=executor,
            role=role,
            title=title or f"Create manifest {tag}",
            env={"DOCKER_CONFIG": docker_config},
            cwd=cwd,
            verify=verify,
        )


class ImagetoolsInspectTask(DockerTask):
    """Inspect a multi-architecture manifest with ``docker buildx imagetools inspect``.

    The ``docker_config`` directory is made available to the subprocess via the
    ``DOCKER_CONFIG`` environment variable so it can read registry credentials
    from the right location.
    """

    def __init__(
        self,
        *,
        reference: str,
        docker_config: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        title: str | None = None,
        cwd: Path | None = None,
        verify: Callable[[TaskResult], None] | None = None,
    ) -> None:
        super().__init__(
            "buildx", "imagetools", "inspect", reference,
            executor=executor,
            role=role,
            title=title or f"Inspect manifest {reference}",
            env={"DOCKER_CONFIG": docker_config},
            cwd=cwd,
            verify=verify,
        )
