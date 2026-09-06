from __future__ import annotations

import shlex
from pathlib import Path

from sonata_tasks.command import CommandTask
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.kubectl import ClusterIpEndpointTask, KubectlTask, k8s_deployment_readiness


def k8s_function_resources_absent(*, function: str, namespace: str,
                                  executor: CommandTaskExecutor, role: str,
                                  timeout_seconds: int = 120,
                                  cwd: Path | None = None) -> CommandTask:
    attempts = max(1, timeout_seconds // 2)
    resources = (f"deployment/fn-{function}", f"service/fn-{function}")
    checks = " ".join(
        f"kubectl -n {shlex.quote(namespace)} get {shlex.quote(resource)} "
        ">/dev/null 2>&1 && present=1;" for resource in resources
    )
    script = (f"for _ in $(seq 1 {attempts}); do present=0; {checks} "
              '[ "$present" -eq 0 ] && exit 0; sleep 2; done; '
              f"echo {shlex.quote(f'function {function} resources did not disappear within {timeout_seconds}s')} >&2; "
              "exit 1")
    return CommandTask(title=f"Wait for {function} resources to disappear",
                       argv=("bash", "-lc", script), executor=executor, role=role,
                       options=CommandOptions(cwd=cwd))


__all__ = ["ClusterIpEndpointTask", "KubectlTask", "k8s_deployment_readiness",
           "k8s_function_resources_absent"]
