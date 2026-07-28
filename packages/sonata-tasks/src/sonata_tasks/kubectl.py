from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import override

from sonata_engine import Task, TaskInputs, TaskOutcome
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


class ClusterIpEndpointTask(Task[str]):
    """Turn the ClusterIP a preceding step read into the URL to talk to.

    A step, not a command: it runs nothing, it interprets. Splitting it from the
    `kubectl get` that fetched the address keeps each step doing one thing, and
    the pair reads in a plan as what it is — look it up, then say where that is.

    Reads `inputs.upstream()`, so it only makes sense inside a `Steps` composite
    whose previous step produced a `TaskResult` carrying the address on stdout.
    """

    def __init__(self, *, service: str, port: int, title: str | None = None) -> None:
        self.title = title or f"Resolve where {service} answers"
        self._service = service
        self._port = port

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        result = inputs.upstream()
        if not isinstance(result, TaskResult):
            raise RuntimeError(
                f"{self.title}: expected the previous step's command result, got {type(result).__name__}"
            )
        address = result.stdout.strip()
        if not address:
            raise RuntimeError(f"service {self._service} reported no ClusterIP")
        return TaskOutcome(value=f"http://{address}:{self._port}")
