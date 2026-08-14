from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.request import urlopen

from sonata_engine import Resource, TaskInputs
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import TaskResult

from sonata_tasks.compensation import best_effort
from sonata_tasks.deployment import REGISTRY_CONTAINER_NAME
from sonata_tasks.docker import DockerTask

RegistryState = Literal["created", "started", "existing"]


def _answers(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/v2/", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def _docker_run(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    *args: str,
    expected: frozenset[int] = frozenset({0}),
) -> TaskResult:
    result = DockerTask(
        *args,
        executor=executor,
        role=role,
        expected_exit_codes=expected,
    ).run(inputs).value
    assert result is not None
    return result


def _registry_state(inspected: TaskResult) -> RegistryState:
    if inspected.return_code == 0 and inspected.stdout.strip() == "true":
        return "existing"
    if inspected.return_code == 0:
        return "started"
    return "created"


def _release(
    inputs: TaskInputs,
    state: RegistryState,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    container: str,
) -> None:
    if state == "created":
        _ = _docker_run(
            inputs,
            executor,
            role,
            "rm",
            "--force",
            container,
            expected=frozenset({0, 1}),
        )
    elif state == "started":
        _ = _docker_run(inputs, executor, role, "stop", container)


def _ensure_running(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    state: RegistryState,
    container: str,
    image: str,
    port: int,
) -> None:
    if state == "started":
        _ = _docker_run(inputs, executor, role, "start", container)
    elif state == "created":
        _ = _docker_run(
            inputs,
            executor,
            role,
            "run",
            "--detach",
            "--restart",
            "unless-stopped",
            "--name",
            container,
            "--publish",
            f"{port}:5000",
            image,
        )


def _wait_ready(
    is_ready: Callable[[], bool],
    readiness_attempts: int,
    readiness_interval: float,
    sleep: Callable[[float], None],
) -> None:
    for attempt in range(readiness_attempts):
        if is_ready():
            return
        if attempt < readiness_attempts - 1:
            sleep(readiness_interval)
    raise RuntimeError("local Docker registry never became ready")


def _cleanup_failed_acquire(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    state: RegistryState,
    error: BaseException,
    container: str,
) -> None:
    if state != "existing":
        best_effort(
            error,
            lambda: _release(inputs, state, executor, role, container),
            what="cleanup for Acquire local registry",
        )


def _acquire(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    container: str,
    image: str,
    port: int,
    is_ready: Callable[[], bool],
    readiness_attempts: int,
    readiness_interval: float,
    sleep: Callable[[float], None],
) -> RegistryState:
    inspected = _docker_run(
        inputs,
        executor,
        role,
        "inspect",
        "--format={{.State.Running}}",
        container,
        expected=frozenset({0, 1}),
    )
    state = _registry_state(inspected)
    try:
        _ensure_running(inputs, executor, role, state, container, image, port)
        _wait_ready(is_ready, readiness_attempts, readiness_interval, sleep)
        return state
    except BaseException as error:
        _cleanup_failed_acquire(inputs, executor, role, state, error, container)
        raise


def docker_registry_resource(
    *,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    container: str = REGISTRY_CONTAINER_NAME,
    image: str = "registry:2",
    port: int = 5000,
    ready: Callable[[], bool] | None = None,
    readiness_attempts: int = 30,
    readiness_interval: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[RegistryState]:
    """A ready local registry that cleans up only lifecycle changes it owns."""
    if readiness_attempts < 1:
        raise ValueError("readiness_attempts must be positive")
    is_ready = ready or (lambda: _answers(port))

    def acquire(inputs: TaskInputs) -> RegistryState:
        return _acquire(
            inputs,
            executor,
            role,
            container,
            image,
            port,
            is_ready,
            readiness_attempts,
            readiness_interval,
            sleep,
        )

    def release(inputs: TaskInputs, state: RegistryState) -> None:
        _release(inputs, state, executor, role, container)

    return Resource(
        title="Acquire local registry",
        acquire=acquire,
        release=release,
        requires=requires,
    )
