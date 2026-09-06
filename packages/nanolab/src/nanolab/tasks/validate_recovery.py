from __future__ import annotations

from sonata_tasks.execution.models import CommandOptions

import json
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import compensated_resource
from nanolab.tasks.compose import DockerComposeProject, WaitForDockerCompose
from sonata_tasks.execution.bindings import CommandTaskExecutor
from nanolab.tasks.execution import ExecutionRole
from nanolab.tasks.http_function import (
    Endpoint,
    HttpFunctionBackendTask,
    HttpFunctionInvokeTask,
    HttpFunctionReplicaStatusTask,
    HttpFunctionSetReplicasTask,
)
from nanolab.tasks.kubectl import KubectlTask
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
                            options=CommandOptions(cwd=cwd),
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
        self._role: ExecutionRole = role
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
                         options=CommandOptions(cwd=self._cwd),
                     )
            ids.extend(_container_ids(_result(listed.run(inputs), self.title).stdout))
        if ids:
            _ = CommandTask(
                    title=self.title,
                    argv=("docker", "rm", "-f", *ids),
                    executor=self._executor,
                    role=self._role,
                    options=CommandOptions(cwd=self._cwd),
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
        self._role: ExecutionRole = role
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
                options=CommandOptions(env=self._project.env, cwd=self._cwd),
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


def _uid(
    resource: str,
    *,
    namespace: str,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None,
    inputs: TaskInputs,
) -> str:
    result = _result(
        KubectlTask(
            "get",
            resource,
            "-o=jsonpath={.metadata.uid}",
            executor=executor,
            role=role,
            namespace=namespace,
            title=f"Read {resource} UID",
            options=CommandOptions(cwd=cwd),
        ).run(inputs),
        f"Read {resource} UID",
    )
    uid = result.stdout.strip()
    if not uid:
        raise RuntimeError(f"{resource}: reported no UID")
    return uid


def _control_plane_pod(stdout: str) -> tuple[str, str, bool]:
    try:
        response = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("invalid control-plane pod response") from error
    items = response.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise RuntimeError("expected exactly one control-plane pod")
    pod = items[0]
    metadata = pod.get("metadata")
    status = pod.get("status")
    if not isinstance(metadata, dict) or not isinstance(status, dict):
        raise RuntimeError("control-plane pod carried no metadata or status")
    name, uid = metadata.get("name"), metadata.get("uid")
    if not isinstance(name, str) or not name or not isinstance(uid, str) or not uid:
        raise RuntimeError("control-plane pod carried no name or UID")
    conditions = status.get("conditions")
    ready = isinstance(conditions, list) and any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    )
    return name, uid, ready


class KubernetesPersistentRecoveryTask(Task[None]):
    """Prove a control-plane pod restart preserves managed K8s resources."""

    def __init__(
        self,
        *,
        name: str,
        payload: str,
        namespace: str,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        timeout_seconds: float = 120,
        poll_seconds: float = 1,
        clock: Callable[[], float] = monotonic,
        sleep_fn: Callable[[float], None] = sleep,
        cwd: Path | None = None,
    ) -> None:
        self.title = f"Recover {name} after Kubernetes control-plane restart"
        self._name = name
        self._payload = payload
        self._namespace = namespace
        self._endpoint = endpoint
        self._executor = executor
        self._role: ExecutionRole = role
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._sleep = sleep_fn
        self._cwd = cwd

    def _pod(self, inputs: TaskInputs) -> tuple[str, str, bool]:
        result = _result(
            KubectlTask(
                "get",
                "pods",
                "-l",
                "app=nanofaas-control-plane",
                "-o",
                "json",
                executor=self._executor,
                role=self._role,
                namespace=self._namespace,
                title="Read control-plane pod",
                options=CommandOptions(cwd=self._cwd),
            ).run(inputs),
            "Read control-plane pod",
        )
        return _control_plane_pod(result.stdout)

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        _ = HttpFunctionSetReplicasTask(
            self._name, replicas=2, endpoint=self._endpoint, executor=self._executor,
            role=self._role, cwd=self._cwd,
        ).run(inputs)
        _ = HttpFunctionReplicaStatusTask(
            self._name, replicas=2, endpoint=self._endpoint, executor=self._executor,
            role=self._role, cwd=self._cwd,
        ).run(inputs)
        deployment = f"deployment/fn-{self._name}"
        service = f"service/fn-{self._name}"
        before = (
            _uid(deployment, namespace=self._namespace, executor=self._executor, role=self._role, cwd=self._cwd, inputs=inputs),
            _uid(service, namespace=self._namespace, executor=self._executor, role=self._role, cwd=self._cwd, inputs=inputs),
        )
        pod_name, pod_uid, ready = self._pod(inputs)
        if not ready:
            raise RuntimeError("control-plane pod is not Ready before restart")
        _ = KubectlTask(
                "delete",
                "pod",
                pod_name,
                "--wait=true",
                executor=self._executor,
                role=self._role,
                namespace=self._namespace,
                title="Restart Kubernetes control plane",
                options=CommandOptions(cwd=self._cwd),
            ).run(inputs)
        deadline = self._clock() + self._timeout_seconds
        while True:
            replacement_name, replacement_uid, replacement_ready = self._pod(inputs)
            if replacement_uid != pod_uid and replacement_ready:
                break
            if self._clock() >= deadline:
                raise RuntimeError("replacement control-plane pod did not become Ready in time")
            self._sleep(self._poll_seconds)
        _ = replacement_name
        _ = HttpFunctionBackendTask(
            self._name, backend="k8s", endpoint=self._endpoint, executor=self._executor,
            role=self._role, cwd=self._cwd,
        ).run(inputs)
        _ = HttpFunctionReplicaStatusTask(
            self._name, replicas=2, endpoint=self._endpoint, executor=self._executor,
            role=self._role, cwd=self._cwd,
        ).run(inputs)
        after = (
            _uid(deployment, namespace=self._namespace, executor=self._executor, role=self._role, cwd=self._cwd, inputs=inputs),
            _uid(service, namespace=self._namespace, executor=self._executor, role=self._role, cwd=self._cwd, inputs=inputs),
        )
        if after != before:
            raise RuntimeError(f"{self._name}: function resource UIDs changed from {before!r} to {after!r}")
        _ = HttpFunctionInvokeTask(
            self._name, payload=self._payload, endpoint=self._endpoint, executor=self._executor,
            role=self._role, cwd=self._cwd,
        ).run(inputs)
        return TaskOutcome(value=None)
