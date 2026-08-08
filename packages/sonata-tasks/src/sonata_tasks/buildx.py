from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sonata_engine import Resource, TaskInputs
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import best_effort


def buildx_builder_resource(
    *,
    name: str,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    requires: tuple[Resource[Any], ...] = (),
    buildkitd_config: str | None = None,
    validate: Callable[[str], None] | None = None,
    replace_existing: bool = False,
) -> Resource[str]:
    """A Docker buildx builder that creates and tears down on demand.

    acquire:
        1. ``docker buildx inspect <name>``
        2. If the builder already exists → reuse it, unless ``replace_existing`` is set.
        3. Otherwise → ``docker buildx create --name <name> --driver docker-container --use``
           then ``docker buildx inspect --bootstrap <name>``, returns ``<name>`` as state.

    release:
        If state is ``"existing"`` → noop.
        Otherwise → ``docker buildx rm --force <name>``.
    """

    def _run(
        inputs: TaskInputs,
        *args: str,
        expected_exit_codes: frozenset[int] = frozenset({0}),
    ) -> TaskResult:
        result = (
            CommandTask(
                title=f"docker buildx {' '.join(args)}",
                argv=("docker", "buildx", *args),
                executor=executor,
                role=role,
                expected_exit_codes=expected_exit_codes,
            )
            .run(inputs)
            .value
        )
        assert result is not None
        return result

    def acquire(inputs: TaskInputs) -> str:
        inspected = _run(inputs, "inspect", name, expected_exit_codes=frozenset({0, 1}))
        if inspected.return_code == 0:
            if not replace_existing:
                if validate is not None:
                    validate(str(inspected.stdout or ""))
                return "existing"
            _ = _run(inputs, "rm", "--force", name)

        create = ["create", "--name", name, "--driver", "docker-container"]
        if buildkitd_config is not None:
            create.extend(("--buildkitd-config", buildkitd_config))
        create.append("--use")
        try:
            _ = _run(inputs, *create)
            bootstrapped = _run(inputs, "inspect", "--bootstrap", name)
            if validate is not None:
                validate(str(bootstrapped.stdout or ""))
        except BaseException as error:
            best_effort(
                error,
                lambda: _run(inputs, "rm", "--force", name),
                what=f"cleanup failed buildx builder {name}",
            )
            raise
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
        always_release=True,
    )
