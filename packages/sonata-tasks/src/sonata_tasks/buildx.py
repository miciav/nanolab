from __future__ import annotations

from typing import Any

from sonata_engine import Resource, TaskInputs
from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask


def buildx_builder_resource(
    *,
    name: str,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[str]:
    """A Docker buildx builder that creates and tears down on demand.

    acquire:
        1. ``docker buildx inspect <name>``
        2. If the builder already exists → state ``"existing"``, no create.
        3. Otherwise → ``docker buildx create --name <name> --driver docker-container --use``
           then ``docker buildx inspect --bootstrap <name>``, returns ``<name>`` as state.

    release:
        If state is ``"existing"`` → noop.
        Otherwise → ``docker buildx rm --force <name>``.
    """

    def _run(
        inputs: TaskInputs, *args: str,
        expected_exit_codes: frozenset[int] = frozenset({0}),
    ) -> TaskResult:
        result = CommandTask(
            title=f"docker buildx {' '.join(args)}",
            argv=("docker", "buildx", *args),
            executor=executor,
            role=role,
            expected_exit_codes=expected_exit_codes,
        ).run(inputs).value
        assert result is not None
        return result

    def acquire(inputs: TaskInputs) -> str:
        inspected = _run(inputs, "inspect", name, expected_exit_codes=frozenset({0, 1}))
        if inspected.return_code == 0:
            return "existing"

        _ = _run(inputs, "create", "--name", name, "--driver", "docker-container", "--use")
        _ = _run(inputs, "inspect", "--bootstrap", name)
        return name

    def release(inputs: TaskInputs, state: str) -> None:
        if state == "existing":
            return
        _ = _run(inputs, "rm", "--force", name)

    return Resource(
        title=f"Acquire {name} buildx builder",
        acquire=acquire,
        release=release,
        requires=requires,
        infrastructure=True,
    )
