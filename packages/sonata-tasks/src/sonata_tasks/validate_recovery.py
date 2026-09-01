from __future__ import annotations

from pathlib import Path
from typing import Any

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.compose import DockerComposeProject, WaitForDockerCompose
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.http_function import (
    Endpoint,
    HttpFunctionBackendTask,
    HttpFunctionInvokeTask,
    HttpFunctionReplicaStatusTask,
    HttpFunctionSetReplicasTask,
)
from sonata_tasks.tasks.models import TaskResult


def _result(outcome: TaskOutcome[TaskResult], title: str) -> TaskResult:
    if outcome.value is None:
        raise RuntimeError(f"{title}: command produced no result")
    return outcome.value


def _container_ids(stdout: str) -> tuple[str, ...]:
    return tuple(sorted(line for line in stdout.splitlines() if line))


class ManagedContainerIdsTask(Task[tuple[str, ...]]):
    """List the two running instances owned by a managed function."""

    def __init__(
        self,
        name: str,
        *,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        self.title = f"Capture managed containers for {name}"
        self._name = name
        self._command = CommandTask(
            title=self.title,
            argv=(
                "docker",
                "ps",
                "-q",
                "--filter",
                "label=io.nanofaas.managed=true",
                "--filter",
                f"label=io.nanofaas.function={name}",
            ),
            executor=executor,
            role=role,
            cwd=cwd,
        )

    def run(self, inputs: TaskInputs) -> TaskOutcome[tuple[str, ...]]:
        ids = _container_ids(_result(self._command.run(inputs), self.title).stdout)
        if len(ids) != 2:
            raise RuntimeError(
                f"{self._name}: expected exactly 2 running managed containers, got {len(ids)}"
            )
        return TaskOutcome(value=ids)


class RemoveManagedContainersTask(Task[None]):
    """Remove standalone managed containers left by an interrupted local run."""

    def __init__(
        self,
        names: tuple[str, ...],
        *,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        self.title = "Clear interrupted managed containers"
        self._names = names
        self._executor = executor
        self._role = role
        self._cwd = cwd

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        ids: list[str] = []
        for name in self._names:
            listed = CommandTask(
                title=self.title,
                argv=(
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    "label=io.nanofaas.managed=true",
                    "--filter",
                    f"label=io.nanofaas.function={name}",
                ),
                executor=self._executor,
                role=self._role,
                cwd=self._cwd,
            )
            ids.extend(_container_ids(_result(listed.run(inputs), self.title).stdout))
        if ids:
            _ = CommandTask(
                title=self.title,
                argv=("docker", "rm", "-f", *ids),
                executor=self._executor,
                role=self._role,
                cwd=self._cwd,
            ).run(inputs)
        return TaskOutcome(value=None)


def managed_container_cleanup_resource(
    names: tuple[str, ...],
    *,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[None]:
    cleanup = RemoveManagedContainersTask(names, executor=executor, role=role, cwd=cwd)
    return compensated_resource(
        title=cleanup.title,
        acquire=lambda inputs: cleanup.run(inputs).value,
        compensate=lambda _inputs: None,
        requires=requires,
    )


class ContainerPersistentRecoveryTask(Task[None]):
    """Prove a local control-plane restart preserves running function instances."""

    def __init__(
        self,
        *,
        name: str,
        payload: str,
        project: DockerComposeProject,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        self.title = f"Recover {name} after control-plane restart"
        self._name = name
        self._payload = payload
        self._project = project
        self._endpoint = endpoint
        self._executor = executor
        self._role = role
        self._cwd = cwd

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        _ = HttpFunctionSetReplicasTask(
            self._name,
            replicas=2,
            endpoint=self._endpoint,
            executor=self._executor,
            role=self._role,
            cwd=self._cwd,
        ).run(inputs)
        _ = HttpFunctionReplicaStatusTask(
            self._name,
            replicas=2,
            endpoint=self._endpoint,
            executor=self._executor,
            role=self._role,
            cwd=self._cwd,
        ).run(inputs)
        before = ManagedContainerIdsTask(
            self._name, executor=self._executor, role=self._role, cwd=self._cwd
        ).run(inputs).value
        _ = CommandTask(
            title="Restart Docker Compose control plane",
            argv=(
                "docker",
                "compose",
                "-f",
                str(self._project.file),
                "-p",
                self._project.name,
                "restart",
                "control-plane",
            ),
            executor=self._executor,
            role=self._role,
            env=self._project.env,
            cwd=self._cwd,
        ).run(inputs)
        _ = WaitForDockerCompose(
            self._project, executor=self._executor, cwd=self._cwd
        ).run(inputs)
        _ = HttpFunctionBackendTask(
            self._name,
            backend="container-local",
            endpoint=self._endpoint,
            executor=self._executor,
            role=self._role,
            cwd=self._cwd,
        ).run(inputs)
        _ = HttpFunctionReplicaStatusTask(
            self._name,
            replicas=2,
            endpoint=self._endpoint,
            executor=self._executor,
            role=self._role,
            cwd=self._cwd,
        ).run(inputs)
        after = ManagedContainerIdsTask(
            self._name, executor=self._executor, role=self._role, cwd=self._cwd
        ).run(inputs).value
        if after != before:
            raise RuntimeError(
                f"{self._name}: managed container IDs changed from {before!r} to {after!r}"
            )
        _ = HttpFunctionInvokeTask(
            self._name,
            payload=self._payload,
            endpoint=self._endpoint,
            executor=self._executor,
            role=self._role,
            cwd=self._cwd,
        ).run(inputs)
        return TaskOutcome(value=None)
