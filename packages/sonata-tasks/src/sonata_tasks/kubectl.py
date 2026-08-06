from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path
from typing import override

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import TaskResult

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


def k8s_deployment_readiness(
    *,
    deployment: str,
    namespace: str,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    timeout_seconds: int = 120,
    cwd: Path | None = None,
) -> tuple[CommandTask, CommandTask]:
    """Wait for a Deployment to exist, then for its rollout to finish.

    Two commands because `kubectl rollout status` fails outright on a Deployment
    that is not there yet, and registering a function only asks the control plane
    to create one — it returns before the object exists.

    Without this, an invocation races the pod's startup. A live run failed with
    `POOL_ERROR: Connection refused` against a Service whose ClusterIP already
    existed: the Service was there, nothing was listening behind it yet.
    """
    attempts = max(1, timeout_seconds // 2)
    appeared = (
        f"for _ in $(seq 1 {attempts}); do "
        f"kubectl -n {shlex.quote(namespace)} get deployment/{shlex.quote(deployment)} "
        ">/dev/null 2>&1 && exit 0; sleep 2; done; "
        f"echo {shlex.quote(f'deployment {deployment} did not appear within {timeout_seconds}s')} >&2; "
        "exit 1"
    )
    return (
        CommandTask(
            title=f"Wait for deployment/{deployment}",
            argv=("bash", "-lc", appeared),
            executor=executor,
            role=role,
            cwd=cwd,
        ),
        KubectlTask(
            "rollout",
            "status",
            f"deployment/{deployment}",
            f"--timeout={timeout_seconds}s",
            executor=executor,
            role=role,
            namespace=namespace,
            title=f"Roll out deployment/{deployment}",
            cwd=cwd,
        ),
    )
