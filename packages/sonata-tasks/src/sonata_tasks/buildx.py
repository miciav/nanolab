from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sonata_engine import Resource, TaskInputs
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import best_effort


def _buildx_builder_run(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
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


def _buildx_builder_create_argv(name: str, buildkitd_config: str | None) -> list[str]:
    create = ["create", "--name", name, "--driver", "docker-container"]
    if buildkitd_config is not None:
        create.extend(("--buildkitd-config", buildkitd_config))
    create.append("--use")
    return create


def _buildx_builder_bootstrap(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    *,
    name: str,
    buildkitd_config: str | None,
    validate: Callable[[str], None] | None,
) -> None:
    create = _buildx_builder_create_argv(name, buildkitd_config)
    try:
        _ = _buildx_builder_run(inputs, executor, role, *create)
        bootstrapped = _buildx_builder_run(inputs, executor, role, "inspect", "--bootstrap", name)
        if validate is not None:
            validate(str(bootstrapped.stdout or ""))
    except BaseException as error:
        best_effort(
            error,
            lambda: _buildx_builder_run(inputs, executor, role, "rm", "--force", name),
            what=f"cleanup failed buildx builder {name}",
        )
        raise


def _buildx_builder_acquire(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    *,
    name: str,
    buildkitd_config: str | None,
    validate: Callable[[str], None] | None,
    replace_existing: bool,
) -> str:
    inspected = _buildx_builder_run(
        inputs, executor, role, "inspect", name, expected_exit_codes=frozenset({0, 1})
    )
    if inspected.return_code == 0:
        if not replace_existing:
            if validate is not None:
                validate(str(inspected.stdout or ""))
            return "existing"
        _ = _buildx_builder_run(inputs, executor, role, "rm", "--force", name)

    _buildx_builder_bootstrap(
        inputs, executor, role, name=name, buildkitd_config=buildkitd_config, validate=validate
    )
    return name


def _buildx_builder_release(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    *,
    name: str,
    state: str,
) -> None:
    if state == "existing":
        return
    _ = _buildx_builder_run(inputs, executor, role, "rm", "--force", name)


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

    def acquire(inputs: TaskInputs) -> str:
        return _buildx_builder_acquire(
            inputs,
            executor,
            role,
            name=name,
            buildkitd_config=buildkitd_config,
            validate=validate,
            replace_existing=replace_existing,
        )

    def release(inputs: TaskInputs, state: str) -> None:
        _buildx_builder_release(inputs, executor, role, name=name, state=state)

    return Resource(
        title=f"Acquire {name} buildx builder",
        acquire=acquire,
        release=release,
        requires=requires,
        always_release=True,
    )
