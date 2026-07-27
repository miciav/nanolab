from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask


class KubectlTask(CommandTask):
    """Run one kubectl sub-command.

    `namespace` is threaded in as `-n` before the sub-command rather than left to
    each caller to append, so an invocation cannot end up namespaced in one place
    and cluster-wide in another. Omit it for the commands that take no namespace,
    such as `version --client`.

    `role` is required: kubectl reads a kubeconfig, and the host's cluster is not
    the one a VM sees.
    """

    def __init__(
        self,
        *args: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        namespace: str | None = None,
        title: str | None = None,
        cwd: Path | None = None,
        verify: Callable[[TaskResult], None] | None = None,
    ) -> None:
        scope = ("-n", namespace) if namespace is not None else ()
        super().__init__(
            title=title or f"kubectl {' '.join(args)}",
            argv=("kubectl", *scope, *args),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify,
        )
