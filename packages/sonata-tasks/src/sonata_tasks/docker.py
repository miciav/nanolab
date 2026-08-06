from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask

# Every task here takes `role` without a default. Building an image on the host
# and building it inside a VM are different daemons, different caches, and the
# result lands somewhere else — a default would hide the one decision the reader
# of a call site most needs to see.


class DockerTask(CommandTask):
    """Run one docker sub-command.

    The catch-all, so a workflow needing something the typed subclasses do not
    cover — `logs`, `rm`, `cp` — still picks a docker task rather than dropping
    to a raw CommandTask and spelling the binary itself.
    """

    def __init__(
        self,
        *args: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        title: str | None = None,
        cwd: Path | None = None,
        verify: Callable[[TaskResult], None] | None = None,
        expected_exit_codes: frozenset[int] = frozenset({0}),
        env: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            title=title or f"docker {' '.join(args)}",
            argv=("docker", *args),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify,
            expected_exit_codes=expected_exit_codes,
            env=env or {},
        )


class DockerBuildTask(DockerTask):
    """Build an image from a Dockerfile."""

    def __init__(
        self,
        *,
        image: str,
        dockerfile: str,
        context: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        title: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            "build",
            "-f",
            dockerfile,
            "-t",
            image,
            context,
            executor=executor,
            role=role,
            title=title or f"Build image {image}",
            cwd=cwd,
        )


class DockerPushTask(DockerTask):
    """Push an image to the registry its tag names."""

    def __init__(
        self,
        *,
        image: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        title: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            "push",
            image,
            executor=executor,
            role=role,
            title=title or f"Push image {image}",
            cwd=cwd,
        )


class DockerInspectTask(DockerTask):
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
        title: str | None = None,
        cwd: Path | None = None,
        verify: Callable[[TaskResult], None] | None = None,
    ) -> None:
        super().__init__(
            "inspect",
            f"--format={fmt}",
            container,
            executor=executor,
            role=role,
            title=title or f"Inspect {container}",
            cwd=cwd,
            verify=verify,
        )
