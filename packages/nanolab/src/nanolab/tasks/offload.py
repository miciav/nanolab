from __future__ import annotations

from sonata_tasks.execution.models import CommandOptions

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sonata_engine import Resource, Steps, Workflow
from sonata_tasks.execution.bindings import (
    CommandTaskExecutor,
    RoleBindings,
    RoleBoundCommandTaskExecutor,
)

from sonata_tasks.command import CommandTask
from sonata_tasks.docker import DockerPushTask
from nanolab.tasks.function import function_resource
from sonata_tasks.gradle import GradleTask
from nanolab.tasks.http_function import (
    HttpFunctionDeleteTask,
    HttpFunctionInvokeTask,
    HttpFunctionRegisterTask,
    HttpStatusCheckTask,
)
from nanolab.tasks.manifest import FunctionManifest
from nanolab.tasks.metrics import PrometheusScrapeCheckTask

OFFLOAD_HEADER = "X-NanoFaaS-Offloaded"


@dataclass(frozen=True, slots=True)
class OffloadFunction:
    """One function registered on both sides of the hop."""

    name: str
    image: str
    payload: str
    build_argv: tuple[str, ...]
    timeout_ms: int = 5000
    concurrency: int = 2
    queue_size: int = 20
    max_retries: int = 3

    def _manifest(self, *, execution_mode: str, offload: dict[str, Any] | None) -> FunctionManifest:
        return FunctionManifest(
            name=self.name,
            image=self.image,
            execution_mode=execution_mode,
            timeout_ms=self.timeout_ms,
            concurrency=self.concurrency,
            queue_size=self.queue_size,
            max_retries=self.max_retries,
            offload=offload,
        )

    def cloud_manifest(self) -> FunctionManifest:
        """The cloud runs the real thing, through the container-local backend."""
        return self._manifest(execution_mode="DEPLOYMENT", offload=None)

    def edge_manifest(self) -> FunctionManifest:
        """The edge holds no implementation: every invocation must be proxied."""
        return self._manifest(execution_mode="LOCAL", offload={"mode": "always"})


@dataclass(frozen=True, slots=True)
class OffloadWorkflowRequest:
    """Everything the offload workflow needs that is not an executor.

    The endpoints are plain strings rather than resource values: both control
    planes are local processes on ports fixed before anything runs, unlike a
    Helm release whose address exists only once its Service does.
    """

    functions: tuple[OffloadFunction, ...]
    cloud_endpoint: str
    edge_endpoint: str
    edge_management: str
    modules: tuple[str, ...] = ("offload", "container-deployment-provider")

    def __post_init__(self) -> None:
        if not self.functions:
            raise ValueError("offload workflow requires at least one function")


def _side(
    function: OffloadFunction,
    *,
    manifest: FunctionManifest,
    endpoint: str,
    side: str,
    executor: CommandTaskExecutor,
    cwd: Path | None,
    requires: tuple[Resource[Any], ...],
) -> Resource[None]:
    return function_resource(
        name=f"{function.name} on the {side}",
        register=HttpFunctionRegisterTask(
            manifest, endpoint=endpoint, executor=executor, role="host", cwd=cwd
        ),
        delete=HttpFunctionDeleteTask(
            function.name, endpoint=endpoint, executor=executor, role="host", cwd=cwd
        ),
        requires=requires,
    )


def _no_local_fallback(
    function: OffloadFunction,
    request: OffloadWorkflowRequest,
    executor: CommandTaskExecutor,
    cwd: Path | None,
) -> Steps:
    """Remove the remote function, then require the edge to answer 502.

    One unit, two steps: the delete is only meaningful as the setup for the
    status check, and selecting it alone would leave the cloud without the
    function and assert nothing. A 200 here would mean the edge quietly computed
    the answer itself — the single hop with no local fallback is the contract.
    """
    return Steps(
        title=f"Verify {function.name} has no local fallback",
        steps=(
            HttpFunctionDeleteTask(
                function.name,
                endpoint=request.cloud_endpoint,
                executor=executor,
                role="host",
                cwd=cwd,
            ),
            HttpStatusCheckTask(
                url=f"{request.edge_endpoint}/v1/functions/{function.name}:invoke",
                expected_status=502,
                payload=function.payload,
                executor=executor,
                role="host",
                title=f"Expect 502 for {function.name}",
                cwd=cwd,
            ),
        ),
    )


def build_offload_workflow(
    request: OffloadWorkflowRequest,
    bindings: RoleBindings,
    *,
    cloud: Callable[[], Resource[Any]],
    edge: Callable[[], Resource[Any]],
    workflow_id: str = "offload",
    cwd: Path | None = None,
    push_function_images: bool = False,
    push_requires: tuple[Resource[Any], ...] = (),
    function_requires: tuple[Resource[Any], ...] = (),
) -> Workflow:
    """Build the offload workflow: two control planes, one hop, three assertions.

    The cloud runs the function; the edge registers it as LOCAL with
    `offload.mode=always`, so every invocation has to come back proxied. The
    legacy version started both control planes by taking the rendered specs
    apart — filtering the two start ids out, building the rest, then inserting
    resources back at the indices they had occupied — and kept the deletes in a
    cleanup list the caller had to remember. Both are resources here.

    `cloud` and `edge` are factories because the processes belong to whoever
    knows where the jar is and which ports are free, not to this package.
    """
    executor = RoleBoundCommandTaskExecutor(bindings)
    workflow = Workflow(workflow_id=workflow_id)
    workflow.add(
        GradleTask(
            ":control-plane:bootJar",
            title="Build control plane",
            executor=executor,
            role="host",
            properties={"controlPlaneModules": ",".join(request.modules)},
            options=CommandOptions(cwd=cwd),
        )
    )
    for function in request.functions:
        workflow.add(
            CommandTask(
                title=f"Build image {function.name}",
                argv=function.build_argv,
                executor=executor,
                role="host",
                options=CommandOptions(cwd=cwd),
            )
        )
        if push_function_images:
            workflow.add(
                DockerPushTask(
                    image=function.image,
                    executor=executor,
                    role="host",
                    options=CommandOptions(cwd=cwd),
                ),
                requires=push_requires,
            )

    planes = (cloud(), edge())
    sides: dict[str, tuple[Resource[None], ...]] = {}
    for function in request.functions:
        registered = tuple(
            _side(
                function,
                manifest=manifest,
                endpoint=endpoint,
                side=side,
                executor=executor,
                cwd=cwd,
                requires=(*planes, *function_requires),
            )
            for manifest, endpoint, side in (
                (function.cloud_manifest(), request.cloud_endpoint, "cloud"),
                (function.edge_manifest(), request.edge_endpoint, "edge"),
            )
        )
        workflow.add(
            HttpFunctionInvokeTask(
                function.name,
                payload=function.payload,
                endpoint=request.edge_endpoint,
                executor=executor,
                role="host",
                require_header=OFFLOAD_HEADER,
                cwd=cwd,
            ),
            requires=(*planes, *registered),
        )
        workflow.add(
            PrometheusScrapeCheckTask(
                url=f"{request.edge_management}/actuator/prometheus",
                executor=executor,
                role="host",
                expect=(
                    f'nanofaas_offload_total{{function="{function.name}",trigger="eager"}}',
                ),
                reject=("nanofaas_offload_failure_total",),
                title=f"Verify {function.name} was offloaded eagerly",
                cwd=cwd,
            ),
            requires=(*planes, *registered),
        )
        sides[function.name] = registered

    # Last, and only for the first function: it deletes the remote copy, so
    # nothing after it could still be offloaded. It declares that function's
    # registrations so the compiler keeps them alive until it has run — without
    # that, the releases land first and the edge answers 404 instead of 502.
    probe = request.functions[0]
    workflow.add(
        _no_local_fallback(probe, request, executor, cwd),
        requires=(*planes, *sides[probe.name]),
    )
    return workflow
