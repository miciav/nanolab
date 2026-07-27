from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask

ResourceSpec = Mapping[str, Any]


def _payload(result: TaskResult, subject: str) -> dict[str, Any]:
    try:
        parsed = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(f"{subject} was not JSON: {result.stdout[:200]!r}") from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{subject} was not a JSON object: {result.stdout[:200]!r}")
    return parsed


def _compare(actual: Mapping[str, Any], expected: Mapping[str, Any], subject: str) -> None:
    """Report the first field that differs, by name.

    The shell version this replaces joined four values into one line and ran
    `test "$actual" = "$expected"`, so a failure told you only that something was
    wrong — never which limit the control plane had failed to apply.
    """
    for field, want in expected.items():
        got = actual.get(field)
        if got != want:
            raise RuntimeError(f"{subject}: {field} is {got!r}, expected {want!r}")


def _halves(resources: ResourceSpec) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    requests = resources.get("requests") or {}
    limits = resources.get("limits") or {}
    if not isinstance(requests, Mapping) or not isinstance(limits, Mapping):
        raise RuntimeError(f"resource spec must hold mappings, got {resources!r}")
    return requests, limits


def container_resource_check(
    *,
    container: str,
    resources: ResourceSpec | None,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> CommandTask:
    """Read a container's host config and assert the declared limits landed on it.

    `role` has no default on purpose: running this on the host and running it
    inside a VM are different checks against different daemons, and a default
    would hide that decision at the call site.

    With no spec the task only reads, which is what the workflow wants when the
    scenario declares no resources: proof the container exists, nothing more.
    """
    verify: Callable[[TaskResult], None] | None = None
    if resources is not None:
        requests, limits = _halves(resources)
        request_memory = int(requests.get("memoryMiB") or 0)
        limit_memory = int(limits.get("memoryMiB") or 0)
        expected = {
            # Docker floors CPU shares at 2; the control plane rounds half up.
            "CpuShares": max(2, int(float(str(requests.get("cpu", 0))) * 1024 + 0.5)),
            "NanoCpus": int(float(str(limits.get("cpu", 0))) * 1_000_000_000),
            # A reservation equal to the limit is left unset rather than restated.
            "MemoryReservation": (
                0 if request_memory == limit_memory else request_memory * 1024 * 1024
            ),
            "Memory": limit_memory * 1024 * 1024,
        }

        def check_container(result: TaskResult) -> None:
            _compare(_payload(result, "container host config"), expected, container)

        verify = check_container

    return CommandTask(
        title=f"Inspect resources of {container}",
        argv=("docker", "inspect", "--format={{json .HostConfig}}", container),
        executor=executor,
        role=role,
        cwd=cwd,
        verify=verify,
    )


def _k8s_cpu(value: object) -> str:
    number = float(str(value))
    return str(int(number)) if number.is_integer() else f"{int(number * 1000)}m"


def k8s_resource_check(
    *,
    deployment: str,
    namespace: str,
    resources: ResourceSpec | None,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> CommandTask:
    """Read a Deployment and assert the declared limits reached its container."""
    verify: Callable[[TaskResult], None] | None = None
    if resources is not None:
        requests, limits = _halves(resources)
        expected = {
            "requests.cpu": _k8s_cpu(requests.get("cpu", 0)),
            "requests.memory": f"{requests.get('memoryMiB', 0)}Mi",
            "limits.cpu": _k8s_cpu(limits.get("cpu", 0)),
            "limits.memory": f"{limits.get('memoryMiB', 0)}Mi",
        }

        def check_deployment(result: TaskResult) -> None:
            payload = _payload(result, "deployment")
            try:
                container = payload["spec"]["template"]["spec"]["containers"][0]
                declared = container["resources"]
            except (KeyError, IndexError, TypeError) as error:
                raise RuntimeError(
                    f"{deployment}: no container resources in the deployment payload"
                ) from error
            actual = {
                f"{half}.{key}": (declared.get(half) or {}).get(key)
                for half in ("requests", "limits")
                for key in ("cpu", "memory")
            }
            _compare(actual, expected, deployment)

        verify = check_deployment

    return CommandTask(
        title=f"Inspect resources of {deployment}",
        argv=("kubectl", "get", "deployment", deployment, "-n", namespace, "-o=json"),
        executor=executor,
        role=role,
        cwd=cwd,
        verify=verify,
    )
