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

    def run(
        inputs: TaskInputs,
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

    def release(inputs: TaskInputs, state: RegistryState) -> None:
        if state == "created":
            _ = run(
                inputs,
                "rm",
                "--force",
                container,
                expected=frozenset({0, 1}),
            )
        elif state == "started":
            _ = run(inputs, "stop", container)

    def acquire(inputs: TaskInputs) -> RegistryState:
        inspected = run(
            inputs,
            "inspect",
            "--format={{.State.Running}}",
            container,
            expected=frozenset({0, 1}),
        )
        if inspected.return_code == 0 and inspected.stdout.strip() == "true":
            state: RegistryState = "existing"
        elif inspected.return_code == 0:
            state = "started"
        else:
            state = "created"

        try:
            if state == "started":
                _ = run(inputs, "start", container)
            elif state == "created":
                _ = run(
                    inputs,
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
            for attempt in range(readiness_attempts):
                if is_ready():
                    return state
                if attempt < readiness_attempts - 1:
                    sleep(readiness_interval)
            raise RuntimeError("local Docker registry never became ready")
        except BaseException as error:
            if state != "existing":
                best_effort(
                    error,
                    lambda: release(inputs, state),
                    what="cleanup for Acquire local registry",
                )
            raise

    return Resource(
        title="Acquire local registry",
        acquire=acquire,
        release=release,
        requires=requires,
    )
