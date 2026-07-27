from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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


class HelmInstallTask(CommandTask):
    """Install or upgrade a Helm release, waiting for it to settle.

    A task in its own right, not only the acquire half of a resource: a workflow
    whose *goal* is to put the platform up wants exactly this and no teardown.
    Sonata never acquires a resource nothing consumes, so that workflow could not
    be expressed with `helm_release_resource` at all.
    """

    def __init__(
        self,
        spec: HelmReleaseSpec,
        *,
        executor: CommandTaskExecutor,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Install Helm release {spec.release}",
            argv=(
                "helm",
                "upgrade",
                "--install",
                spec.release,
                spec.chart,
                "--namespace",
                spec.namespace,
                "--create-namespace",
                "--wait",
                "--timeout",
                spec.timeout,
                *spec.values,
            ),
            executor=executor,
            role=spec.role,
            cwd=cwd,
        )


class HelmUninstallTask(CommandTask):
    """Remove a Helm release. Tolerates an absent release so cleanup is idempotent."""

    def __init__(
        self,
        spec: HelmReleaseSpec,
        *,
        executor: CommandTaskExecutor,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Uninstall Helm release {spec.release}",
            argv=(
                "helm",
                "uninstall",
                spec.release,
                "--namespace",
                spec.namespace,
                "--ignore-not-found",
                "--wait",
            ),
            executor=executor,
            role=spec.role,
            cwd=cwd,
        )


def helm_release_resource(
    spec: HelmReleaseSpec,
    *,
    executor: CommandTaskExecutor,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[HelmReleaseSpec]:
    """A Helm release as an infrastructure resource, composed from the two tasks.

    The commands live in `HelmInstallTask`/`HelmUninstallTask`; what this adds is
    the lifecycle around them — the compiler placing them around the consumers,
    and the best-effort uninstall for an install that deployed something before
    failing, which has to happen here because the engine never releases an
    acquire that did not pass.
    """
    install = HelmInstallTask(spec, executor=executor)
    uninstall = HelmUninstallTask(spec, executor=executor)

    def acquire(inputs: TaskInputs) -> HelmReleaseSpec:
        try:
            _ = install.run(inputs)
        except BaseException as error:
            try:
                _ = uninstall.run(inputs)
            except BaseException as cleanup_error:
                error.add_note(
                    "Best-effort Helm uninstall after a failed install failed: "
                    f"{cleanup_error}"
                )
            raise
        return spec

    def release(inputs: TaskInputs, _acquired: HelmReleaseSpec) -> None:
        _ = uninstall.run(inputs)

    return Resource(
        title=f"Acquire Helm release {spec.release}",
        acquire=acquire,
        release=release,
        requires=requires,
        infrastructure=True,
    )
