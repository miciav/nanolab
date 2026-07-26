from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sonata_engine import Resource, TaskInputs
from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole

from sonata_tasks.command import CommandTask


@dataclass(frozen=True, slots=True)
class HelmReleaseSpec:
    """The inputs needed to manage one Helm release."""

    release: str
    chart: str
    namespace: str
    values: tuple[str, ...]
    role: ExecutionRole = "stack"
    timeout: str = "5m"


def helm_release_resource(
    spec: HelmReleaseSpec,
    *,
    executor: CommandTaskExecutor,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[HelmReleaseSpec]:
    """Expose a Helm release as an infrastructure resource."""

    def install(current: HelmReleaseSpec, inputs: TaskInputs) -> None:
        _ = CommandTask(
            title=f"Install Helm release {current.release}",
            argv=(
                "helm",
                "upgrade",
                "--install",
                current.release,
                current.chart,
                "--namespace",
                current.namespace,
                "--create-namespace",
                "--wait",
                "--timeout",
                current.timeout,
                *current.values,
            ),
            executor=executor,
            role=current.role,
        ).run(inputs)

    def uninstall(current: HelmReleaseSpec, inputs: TaskInputs) -> None:
        _ = CommandTask(
            title=f"Uninstall Helm release {current.release}",
            argv=(
                "helm",
                "uninstall",
                current.release,
                "--namespace",
                current.namespace,
                "--ignore-not-found",
                "--wait",
            ),
            executor=executor,
            role=current.role,
        ).run(inputs)

    def acquire(inputs: TaskInputs) -> HelmReleaseSpec:
        try:
            install(spec, inputs)
        except BaseException as error:
            try:
                uninstall(spec, inputs)
            except BaseException as cleanup_error:
                error.add_note(
                    "Best-effort Helm uninstall after a failed install failed: "
                    f"{cleanup_error}"
                )
            raise
        return spec

    def release(inputs: TaskInputs, acquired: HelmReleaseSpec) -> None:
        uninstall(acquired, inputs)

    return Resource(
        title=f"Acquire Helm release {spec.release}",
        acquire=acquire,
        release=release,
        requires=requires,
        infrastructure=True,
    )
