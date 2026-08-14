"""Bootstrap operations: run remote command plans against a provisioned VM."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from sonata_tasks.components.bootstrap import retarget_bootstrap_operation
from sonata_tasks.components.context import ScenarioExecutionContext
from sonata_tasks.components.operations import RemoteCommandOperation
from sonata_tasks.components.operations import ScenarioOperation
from sonata_tasks.deployment import LOCAL_REGISTRY
from sonata_tasks.shell import SubprocessShell
from sonata_tasks.tasks.executors import HostCommandRunner, HostCommandTaskExecutor
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult
from sonata_tasks.vm.azure import AzureVmProvider
from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.proxmox import ProxmoxVmProvider
from sonata_engine.workflow.reporting import subtask


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
        local_registry=LOCAL_REGISTRY,
        resolved_scenario=None,
        vm_request=request,
        cleanup_vm=False,
        assets_root=assets_root,
    )


@dataclass
class OperationTask:
    """Runs one RemoteCommandOperation through an executor, raising on failure."""

    task_id: str
    title: str
    spec: CommandTaskSpec
    executor: HostCommandTaskExecutor

    def run(self) -> TaskResult:
        result = self.executor.run(self.spec)
        if result.status != "passed":
            detail = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part) or "no output"
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
    spec = CommandTaskSpec(
        task_id=operation.operation_id,
        summary=operation.summary,
        argv=tuple(operation.argv),
        # The components speak of a "target"; the execution layer speaks of a
        # role. Translating here keeps that vocabulary at the edge instead of
        # carrying both words through every spec.
        role="stack" if operation.execution_target == "vm" else "host",
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
    runner = cast(
        HostCommandRunner, getattr(provider, "shell", None) or SubprocessShell()
    )
    # Materialised because it is inspected before it is run, and a generator
    # would arrive at the loop already spent.
    planned = tuple(operations)
    # Bootstrap holds one executor, and it runs here. An operation that asks for
    # the VM has no way to get there, so say so rather than running it locally:
    # this used to be caught by a mismatched target deep in the executor.
    inside_vm = [
        operation.operation_id
        for operation in planned
        if operation.execution_target == "vm"
    ]
    if inside_vm:
        raise ValueError(
            "bootstrap runs on the host, but these operations ask for the VM: "
            + ", ".join(inside_vm)
        )
    executor = HostCommandTaskExecutor(runner)
    tasks: list[OperationTask] = [
        operation_task(
            replace(operation, operation_id=f"provision.{role}.{operation.operation_id}"),
            executor,
        )
        for operation in planned
    ]
    for task in tasks:
        with subtask(task_id=task.task_id, title=task.title):
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
    request = context.vm_request
    if not isinstance(orchestrator, (AzureVmProvider, ProxmoxVmProvider)):
        # Multipass plans are built against the synthetic "{name}.internal"
        # host; re-point them at the post-ensure connection host (for external
        # the planner already used the configured host, so this is identity).
        # When resolution still yields the placeholder there is no real host
        # to substitute, and operations the shim retargeted pre-ensure via
        # cloud endpoint helpers must not be overwritten.
        if request.host is None or request.host == f"{request.name}.internal":
            return tuple(operations)
        return tuple(
            retarget_bootstrap_operation(
                operation,
                context=context,
                host=request.host,
                port=None,
                private_key=None,
            )
            for operation in operations
        )

    if isinstance(orchestrator, ProxmoxVmProvider):
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
