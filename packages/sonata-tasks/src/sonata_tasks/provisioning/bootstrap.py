"""Bootstrap operations: run remote command plans against a provisioned VM."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from sonata_tasks.components.bootstrap import retarget_bootstrap_operation
from sonata_tasks.components.context import ScenarioExecutionContext
from sonata_tasks.components.operations import RemoteCommandOperation
from sonata_tasks.components.operations import ScenarioOperation
from sonata_tasks.shell import SubprocessShell
from sonata_tasks.tasks.executors import HostCommandTaskExecutor
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult
from sonata_tasks.vm.models import VmRequest
from sonata_tasks.workflow.reporting import workflow_step


def scenario_context(
    repo_root: Path,
    request: VmRequest,
    assets_root: Path,
) -> ScenarioExecutionContext:
    return ScenarioExecutionContext(
        repo_root=repo_root,
        scenario_name="provision",
        runtime="java",
        namespace=None,
        local_registry="localhost:5000",
        resolved_scenario=None,
        vm_request=request,
        cleanup_vm=False,
        assets_root=assets_root,
    )


@dataclass
class OperationTask:
    """A legacy-Engine Task running one RemoteCommandOperation via an executor."""

    task_id: str
    title: str
    spec: CommandTaskSpec
    executor: HostCommandTaskExecutor

    def run(self) -> TaskResult:
        result = self.executor.run(self.spec)
        if result.status != "passed":
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise RuntimeError(
                f"{self.task_id} failed (exit {result.return_code}): {detail}"
            )
        return result


def operation_task(
    operation: RemoteCommandOperation,
    executor: HostCommandTaskExecutor,
    *,
    title: str | None = None,
) -> OperationTask:
    target = "vm" if operation.execution_target == "vm" else "host"
    spec = CommandTaskSpec(
        task_id=operation.operation_id,
        summary=operation.summary,
        argv=tuple(operation.argv),
        target=target,
        env=dict(operation.env),
        remote_dir=None,
    )
    return OperationTask(
        task_id=spec.task_id,
        title=title if title is not None else spec.summary,
        spec=spec,
        executor=executor,
    )


def run_bootstrap_operations(
    provider: object,
    operations: Iterable[RemoteCommandOperation],
    *,
    role: str,
) -> None:
    runner = getattr(provider, "shell", None) or SubprocessShell()
    executor = HostCommandTaskExecutor(runner)
    tasks = [
        operation_task(
            replace(operation, operation_id=f"provision.{role}.{operation.operation_id}"),
            executor,
        )
        for operation in operations
    ]
    for task in tasks:
        with workflow_step(task_id=task.task_id, title=task.title):
            task.run()


def remote_operations(
    operations: Iterable[ScenarioOperation],
) -> tuple[RemoteCommandOperation, ...]:
    resolved: list[RemoteCommandOperation] = []
    for operation in operations:
        if not isinstance(operation, RemoteCommandOperation):
            raise TypeError(f"bootstrap operation is not a remote command: {operation}")
        resolved.append(operation)
    return tuple(resolved)


def retarget_cloud_operations(
    orchestrator: object,
    context: ScenarioExecutionContext,
    operations: Iterable[RemoteCommandOperation],
) -> tuple[RemoteCommandOperation, ...]:
    if context.vm_request.lifecycle not in {"azure", "proxmox"}:
        return tuple(operations)

    request = context.vm_request
    if context.vm_request.lifecycle == "proxmox":
        host, port = orchestrator.ssh_endpoint(request)
    else:
        host, port = orchestrator.connection_host(request), None
    private_key = orchestrator.ssh_private_key_path(request)
    return tuple(
        retarget_bootstrap_operation(
            operation,
            context=context,
            host=host,
            port=port,
            private_key=private_key,
        )
        for operation in operations
    )
