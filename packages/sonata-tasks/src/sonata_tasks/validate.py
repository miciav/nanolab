from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sonata_engine import Resource, Workflow
from sonata_tasks.execution.bindings import (
    CommandTaskExecutor,
    RoleBindings,
    RoleBoundCommandTaskExecutor,
)

from sonata_tasks.command import CommandTask
from sonata_tasks.http_function import HttpFunctionInvokeTask
from sonata_tasks.platform import (
    PlatformFunction,
    PlatformRequest,
    add_platform,
)
from sonata_tasks.resources import ContainerResourceCheckTask, K8sResourceCheckTask

# `validate` is the platform half plus a question asked of it: does the function
# answer, and did the limits it declared reach the object that runs it.
ValidateFunction = PlatformFunction
ValidateWorkflowRequest = PlatformRequest


def _inspection_task(
    request: PlatformRequest,
    function: PlatformFunction,
    executor: CommandTaskExecutor,
    cwd: Path | None,
) -> CommandTask:
    """Read the object the backend created and assert the declared limits reached it.

    Deliberately asks the backend, not the control plane: the control plane would
    report what it was told, while the point of this check is that the values
    landed on the real Deployment or container.
    """
    if request.backend == "k8s":
        return K8sResourceCheckTask(
            deployment=f"fn-{function.name}",
            namespace=request.namespace,
            resources=function.resources,
            executor=executor,
            role=request.role,
            cwd=cwd,
        )
    return ContainerResourceCheckTask(
        container=f"nanofaas-{function.name}-r1",
        resources=function.resources,
        executor=executor,
        role=request.role,
        cwd=cwd,
    )


def build_validate_workflow(
    request: PlatformRequest,
    bindings: RoleBindings,
    *,
    workflow_id: str = "validate",
    cwd: Path | None = None,
    control_plane_process: Callable[[], Resource[Any]] | None = None,
    local_endpoint: str = "http://127.0.0.1:18080",
    requires: tuple[Resource[Any], ...] = (),
) -> Workflow:
    """Build the validate workflow: build, deploy, register, invoke, inspect.

    The legacy version was three functions — tasks, cleanup, and a separate k8s
    deployment list — and the caller had to remember to run the cleanup. Here the
    teardown is the release half of the resources, which the compiler places
    itself, runs in reverse, and runs even when something fails.

    `control_plane_process` supplies the container backend's local control plane;
    it is a factory rather than a resource so a k8s run never constructs one.
    """
    executor = RoleBoundCommandTaskExecutor(bindings)
    workflow = Workflow(workflow_id=workflow_id)
    platform = add_platform(
        workflow,
        request,
        executor=executor,
        cwd=cwd,
        control_plane_process=control_plane_process,
        local_endpoint=local_endpoint,
        requires=requires,
    )

    for function, registered in zip(request.functions, platform.functions):
        workflow.add(
            HttpFunctionInvokeTask(
                function.name,
                payload=function.payload,
                endpoint=platform.endpoint,
                executor=executor,
                role=request.role,
                cwd=cwd,
            ),
            requires=(*requires, *platform.resources, registered),
        )
        workflow.add(
            _inspection_task(request, function, executor, cwd),
            requires=(*requires, registered),
        )
    return workflow
