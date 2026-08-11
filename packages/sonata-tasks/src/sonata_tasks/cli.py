from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sonata_engine import Resource, Workflow
from sonata_tasks.execution.bindings import (
    CommandTaskExecutor,
    RoleBindings,
    RoleBoundCommandTaskExecutor,
)
from sonata_tasks.execution.roles import ExecutionRole

from sonata_tasks.cli_function import (
    CliFunctionApplyTask,
    CliFunctionDeleteTask,
    CliFunctionInvokeTask,
)
from sonata_tasks.command import CommandTask
from sonata_tasks.deployment import DEFAULT_NAMESPACE
from sonata_tasks.docker import DockerPushTask
from sonata_tasks.function import function_resource
from sonata_tasks.gradle import GradleTask
from sonata_tasks.kubectl import k8s_deployment_readiness
from sonata_tasks.manifest import FunctionManifest


@dataclass(frozen=True, slots=True)
class CliFunction:
    """One function the CLI workflow registers, invokes, and removes."""

    name: str
    image: str
    payload: str
    resources: dict[str, object] | None = None
    build_argv: tuple[str, ...] | None = None
    image_build_argv: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class CliWorkflowRequest:
    """Everything the CLI workflow needs that is not an executor."""

    functions: tuple[CliFunction, ...]
    cli_role: ExecutionRole = "host"
    build_role: ExecutionRole = "host"
    namespace: str = DEFAULT_NAMESPACE
    endpoint: str = "http://127.0.0.1:8080"
    binary: str = "clients/cli/build/install/nanofaas-cli/bin/nanofaas-cli"
    push_function_images: bool = False

    def __post_init__(self) -> None:
        if self.cli_role not in ("host", "stack"):
            raise ValueError("CLI workflow can run only on host or stack")
        if self.build_role not in ("host", "stack"):
            raise ValueError("CLI workflow can run only on host or stack")
        if not self.functions:
            raise ValueError("CLI workflow requires at least one function")


def _cli_argv(request: CliWorkflowRequest, *arguments: str) -> tuple[str, ...]:
    return (
        request.binary,
        "--endpoint",
        request.endpoint,
        "--namespace",
        request.namespace,
        *arguments,
    )


def _function_resource(
    request: CliWorkflowRequest,
    function: CliFunction,
    executor: CommandTaskExecutor,
    cwd: Path | None,
    *,
    readiness_timeout_seconds: int | None = None,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[None]:
    """The registered function as an acquire/release pair.

    Only the commands are CLI-specific: applying a manifest through the nanofaas
    binary. The lifecycle around them — splicing, compensation on a partial
    register, readiness — belongs to `function_resource`, which every other
    workflow reuses with its own register and delete commands.
    """
    manifest = FunctionManifest(
        name=function.name, image=function.image, resources=function.resources
    )
    prefix = _cli_argv(request)
    apply_task = CliFunctionApplyTask(
        manifest, cli_argv=prefix, executor=executor, role=request.cli_role, cwd=cwd
    )
    delete_task = CliFunctionDeleteTask(
        function.name, cli_argv=prefix, executor=executor, role=request.cli_role, cwd=cwd
    )
    ready_tasks = (
        k8s_deployment_readiness(
            deployment=f"fn-{function.name}",
            namespace=request.namespace,
            executor=executor,
            role=request.cli_role,
            timeout_seconds=readiness_timeout_seconds,
            cwd=cwd,
        )
        if readiness_timeout_seconds is not None
        else ()
    )

    return function_resource(
        name=function.name,
        register=apply_task,
        delete=delete_task,
        readiness=ready_tasks,
        requires=requires,
    )


def build_cli_workflow(
    request: CliWorkflowRequest,
    bindings: RoleBindings,
    *,
    workflow_id: str = "cli",
    cwd: Path | None = None,
    control_plane_build_argv: tuple[str, ...] | None = None,
    requires: tuple[Resource[Any], ...] = (),
    bootstrap: tuple[CommandTask, ...] = (),
    bootstrap_requires: tuple[Resource[Any], ...] = (),
    function_requires: tuple[Resource[Any], ...] = (),
    push_requires: tuple[Resource[Any], ...] = (),
    readiness_timeout_seconds: int | None = None,
) -> Workflow:
    """Build the CLI end-to-end workflow: build, register, list, invoke, remove.

    Build commands do not require the control plane. `requires` wraps only commands
    that use its API, so selection of a build task stays offline.

    `bootstrap` is an ordered sequence of tasks (e.g. VM provisioning) spliced in
    after the build tasks and before the CLI itself runs; `bootstrap_requires`
    are the resources those tasks declare (typically a VM resource), which also
    controls where that resource's acquire/release land.

    `function_requires` (e.g. a Helm release) becomes a real dependency of every
    function's own resource, so a slice that keeps one function keeps it too.
    """
    executor = RoleBoundCommandTaskExecutor(bindings)
    workflow = Workflow(workflow_id=workflow_id)
    workflow.add(
        GradleTask(
            ":nanofaas-cli:installDist",
            title="Build nanofaas-cli",
            executor=executor,
            role=request.build_role,
            cwd=cwd,
        )
    )
    if control_plane_build_argv is not None:
        workflow.add(
            CommandTask(
                title="Build local control plane",
                argv=control_plane_build_argv,
                executor=executor,
                role=request.build_role,
                cwd=cwd,
            )
        )
    for function in request.functions:
        if function.build_argv is not None:
            workflow.add(
                CommandTask(
                    title=(
                        f"Build application artifact: {function.name}"
                        if function.image_build_argv is not None
                        else f"Build image {function.name}"
                    ),
                    argv=function.build_argv,
                    executor=executor,
                    role=request.build_role,
                    cwd=cwd,
                )
            )
        if function.image_build_argv is not None:
            workflow.add(
                CommandTask(
                    title=f"Build image {function.name}",
                    argv=function.image_build_argv,
                    executor=executor,
                    role=request.build_role,
                    cwd=cwd,
                )
            )
        if request.push_function_images and function.build_argv is not None:
            workflow.add(
                DockerPushTask(
                    image=function.image,
                    executor=executor,
                    role=request.build_role,
                    title=f"Push image {function.image}",
                    cwd=cwd,
                ),
                requires=push_requires,
            )
    for bootstrap_task in bootstrap:
        workflow.add(bootstrap_task, requires=bootstrap_requires)
    resources = tuple(
        _function_resource(
            request,
            function,
            executor,
            cwd,
            readiness_timeout_seconds=readiness_timeout_seconds,
            requires=function_requires,
        )
        for function in request.functions
    )
    workflow.add(
        CommandTask(
            title="List functions",
            argv=_cli_argv(request, "fn", "list"),
            executor=executor,
            role=request.cli_role,
            cwd=cwd,
        ),
        requires=(*requires, *resources),
    )
    for function, resource in zip(request.functions, resources):
        workflow.add(
            CliFunctionInvokeTask(
                function.name,
                payload=function.payload,
                cli_argv=_cli_argv(request),
                executor=executor,
                role=request.cli_role,
                cwd=cwd,
            ),
            requires=(*requires, resource),
        )
    return workflow
