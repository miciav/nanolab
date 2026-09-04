"""The control plane's own account of a run, taken while the cluster still exists.

Four different reasons why the internal scaler never evaluates a function -
module absent, no deployment coordinator, no managed target, an exception
swallowed per tick - are indistinguishable in metrics and one line apart in this
log. The VM is destroyed at teardown, so it has to be taken during the run or
not at all.

It is captured from stdout rather than written remotely and copied back: kubectl
runs on the cluster's own role, while the run directory belongs to whichever
host drives the load, and a run once wrote the log on one VM and looked for it
on the other.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from sonata_tasks.command import CommandTask
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import TaskResult

CONTROL_PLANE_DEPLOYMENT = "deploy/nanofaas-control-plane"


def write_control_plane_log(destination: Path, result: TaskResult) -> None:
    """Keep the log the run was collected for, next to the rest of the evidence."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    _ = destination.write_text(result.stdout)


def collect_control_plane_log(
    *,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    namespace: str,
    destination: Path,
    remote_dir: str | None = None,
    title: str = "Collect the control-plane log",
) -> CommandTask:
    """The command, bound to the cluster that answers for it."""
    return CommandTask(
        title=title,
        argv=(
            "bash",
            "-lc",
            f"sudo kubectl -n {namespace} logs {CONTROL_PLANE_DEPLOYMENT} --tail=-1",
        ),
        executor=executor,
        role=role,
        remote_dir=remote_dir,
        verify=partial(write_control_plane_log, destination),
    )
