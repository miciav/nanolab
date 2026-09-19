"""Per-run rootless containerd resources used by the scenario plans."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sonata_engine import Resource, TaskInputs
from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.execution.bindings import CommandTaskExecutor

from nanolab.config.environment import EnvironmentConfig
from nanolab.tasks.execution import ExecutionRole


@dataclass(frozen=True, slots=True)
class RootlessRun:
    """The one test-owned runtime identified by its unique run token."""

    run_id: str
    repo_root: Path
    script: Path


def run_for_environment(
    repo_root: Path,
    tool_root: Path,
    environment: EnvironmentConfig | None,
) -> RootlessRun:
    """Resolve absolute paths on the machine running the rootless daemon."""
    if environment is not None and environment.provider != "local":
        home = Path(environment.target("stack").remote_home)
        return RootlessRun(
            uuid4().hex[:12],
            home / "nanofaas",
            home / "nanolab-assets/containerd-rootless/session.sh",
        )
    return RootlessRun(
        uuid4().hex[:12],
        repo_root,
        tool_root / "assets/containerd-rootless/session.sh",
    )


def _resource(
    run: RootlessRun,
    *,
    name: str,
    start: str,
    stop: str,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    requires: tuple[Resource[Any], ...] = (),
    start_args: tuple[str, ...] = (),
) -> Resource[RootlessRun]:
    acquire = CommandTask(
        title=f"Start {name}",
        argv=(
            "bash",
            str(run.script),
            start,
            run.run_id,
            str(run.repo_root),
            *start_args,
        ),
        executor=executor,
        role=role,
    )
    release = CommandTask(
        title=f"Stop {name}",
        argv=("bash", str(run.script), stop, run.run_id, str(run.repo_root)),
        executor=executor,
        role=role,
    )

    def start_run(inputs: TaskInputs) -> RootlessRun:
        _ = acquire.run(inputs)
        return run

    return compensated_resource(
        title=f"Acquire {name}",
        acquire=start_run,
        compensate=release.run,
        requires=requires,
    )


def registry_resource(
    run: RootlessRun,
    *,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[RootlessRun]:
    """Start and later remove the registry and port owned by one run."""
    return _resource(
        run,
        name="rootless containerd test registry",
        start="registry-start",
        stop="registry-stop",
        executor=executor,
        role=role,
        requires=requires,
    )


def control_plane_resource(
    run: RootlessRun,
    *,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    requires: tuple[Resource[Any], ...] = (),
    cpuset_cores: int = 0,
    budget: str = "",
    mode: str | None = None,
    artifact: Path | None = None,
    cpu: float | None = None,
    memory_bytes: int | None = None,
) -> Resource[RootlessRun]:
    """Start and later stop the control-plane unit and owned publications."""
    if artifact is not None and (
        not artifact.is_absolute()
        or mode not in {"jvm", "native"}
        or cpu is None
        or cpu <= 0
        or memory_bytes is None
        or memory_bytes <= 0
    ):
        raise ValueError(
            "soak control plane requires absolute artifact and positive limits"
        )
    extra = (
        (mode, str(artifact), str(cpu), str(memory_bytes))
        if artifact is not None
        and mode is not None
        and cpu is not None
        and memory_bytes is not None
        else ()
    )
    return _resource(
        run,
        name="rootless containerd test runtime",
        start="control-start",
        stop="control-stop",
        executor=executor,
        role=role,
        requires=requires,
        start_args=(str(cpuset_cores), budget, *extra),
    )


def prometheus_resource(
    run: RootlessRun,
    *,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[RootlessRun]:
    """Collect the same control-plane series as the existing load workloads."""
    return _resource(
        run,
        name="rootless Prometheus",
        start="prometheus-start",
        stop="prometheus-stop",
        executor=executor,
        role=role,
        requires=requires,
    )
