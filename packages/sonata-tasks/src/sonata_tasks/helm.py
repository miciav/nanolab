from __future__ import annotations

from collections.abc import Mapping
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
    values: Mapping[str, str]
    role: ExecutionRole = "stack"
    timeout: str = "5m"


def _set_args(values: Mapping[str, str]) -> tuple[str, ...]:
    args: list[str] = []
    for key, value in values.items():
        args.extend(("--set", f"{key}={value}"))
    return tuple(args)


def helm_release_resource(
    spec: HelmReleaseSpec,
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
                *_set_args(current.values),
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
