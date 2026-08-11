from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sonata_engine import Resource, Steps, Workflow
from sonata_tasks.execution.bindings import (
    CommandTaskExecutor,
    RoleBindings,
    RoleBoundCommandTaskExecutor,
)

from sonata_tasks.command import CommandTask
from sonata_tasks.http_function import HttpExecutionSuccessTask, HttpFunctionEnqueueTask, HttpFunctionInvokeTask
from sonata_tasks.k6 import K6Task
from sonata_tasks.loadtest.models import K6Config
from sonata_tasks.metrics import PrometheusMinimumCheckTask
from sonata_tasks.platform import (
    PlatformFunction,
    PlatformRequest,
    add_platform,
)
from sonata_tasks.resources import ContainerResourceCheckTask, K8sResourceCheckTask

# `validate` is the platform half plus a question asked of it: does the function
# answer, and did the limits it declared reach the object that runs it.
ValidateFunction = PlatformFunction


@dataclass(frozen=True, slots=True)
class ValidateWorkflowRequest(PlatformRequest):
    """The common platform request plus the K8s-only queue probe."""

    queue_probe: PlatformFunction | None = None
    extended_k8s_checks: bool = False
    queue_burst_script: Path | None = None


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
    # ValidateWorkflowRequest, not PlatformRequest: this reads queue_probe,
    # extended_k8s_checks and queue_burst_script, none of which exist on the
    # base. Every caller already passes the subclass — the wider annotation only
    # hid that from the checker.
    request: ValidateWorkflowRequest,
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
    platform_request = (
        replace(request, functions=(*request.functions, request.queue_probe))
        if request.queue_probe is not None
        else request
    )
    platform = add_platform(
        workflow,
        platform_request,
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
        if request.backend == "k8s" and request.extended_k8s_checks:
            workflow.add(
                Steps(
                    title=f"Verify async lifecycle of {function.name}",
                    steps=(
                        HttpFunctionEnqueueTask(function.name, payload=function.payload, endpoint=platform.endpoint, executor=executor, role=request.role, idempotency_key=f"{function.name}-idempotent", title=f"Enqueue {function.name}", cwd=cwd),
                        HttpFunctionEnqueueTask(function.name, payload=function.payload, endpoint=platform.endpoint, executor=executor, role=request.role, idempotency_key=f"{function.name}-idempotent", match_upstream=True, title=f"Repeat enqueue {function.name}", cwd=cwd),
                        HttpExecutionSuccessTask(endpoint=platform.endpoint, executor=executor, role=request.role, cwd=cwd),
                    ),
                ),
                requires=(*requires, *platform.resources, registered),
            )
            workflow.add(
                PrometheusMinimumCheckTask(
                    url=platform.endpoint,
                    minimums=(("function_enqueue_total", {"function": function.name}, 1), ("function_success_total", {"function": function.name}, 1)),
                    executor=executor,
                    role=request.role,
                    title=f"Check metrics for {function.name}",
                    cwd=cwd,
                ),
                requires=(*requires, *platform.resources, registered),
            )
    if request.queue_probe is not None:
        queue_registered = platform.functions[len(request.functions)]
        workflow.add(
            HttpFunctionInvokeTask(
                request.queue_probe.name,
                payload=request.queue_probe.payload,
                endpoint=platform.endpoint,
                executor=executor,
                role=request.role,
                cwd=cwd,
            ),
            requires=(*requires, *platform.resources, queue_registered),
        )
        if request.queue_burst_script is not None:
            workflow.add(
                CommandTask(
                    title="Check k6 is usable",
                    argv=("k6", "version"),
                    executor=executor,
                    role=request.role,
                ),
                requires=(*requires, *platform.resources, queue_registered),
            )
            workflow.add(
                K6Task(
                    K6Config(
                        script_path=request.queue_burst_script,
                        target_url=platform.endpoint,
                        summary_output_path=Path("/tmp/nanolab-k8s-queue-burst.json"),
                        vus=12,
                        duration="2s",
                        env={
                            "NANOFAAS_FUNCTION": request.queue_probe.name,
                            "NANOFAAS_PAYLOAD": request.queue_probe.payload,
                        },
                    ),
                    executor=executor,
                    role=request.role,
                    remote_dir=".",
                    title="Burst the synchronous queue",
                    require_pass=True,
                ),
                requires=(*requires, *platform.resources, queue_registered),
            )
    return workflow
