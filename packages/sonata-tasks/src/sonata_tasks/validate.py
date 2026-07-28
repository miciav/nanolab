from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from sonata_engine import Resource, Steps, Workflow
from workflow_tasks.execution.bindings import (
    CommandTaskExecutor,
    RoleBindings,
    RoleBoundCommandTaskExecutor,
)
from workflow_tasks.execution.roles import ExecutionRole

from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.docker import DockerBuildTask, DockerPushTask
from sonata_tasks.function import function_resource
from sonata_tasks.gradle import GradleTask
from sonata_tasks.helm import HelmInstallTask, HelmReleaseSpec, HelmUninstallTask
from sonata_tasks.http_function import (
    Endpoint,
    HttpFunctionDeleteTask,
    HttpFunctionInvokeTask,
    HttpFunctionRegisterTask,
)
from sonata_tasks.kubectl import ClusterIpEndpointTask, KubectlTask
from sonata_tasks.manifest import FunctionManifest
from sonata_tasks.resources import ContainerResourceCheckTask, K8sResourceCheckTask

Backend = Literal["container", "k8s"]
Build = Literal["docker", "buildpack"]

CONTROL_PLANE_SERVICE = "control-plane"
CONTROL_PLANE_PORT = 8080

_MODULES: dict[Backend, str] = {
    "container": "container-deployment-provider",
    "k8s": "k8s-deployment-provider",
}


@dataclass(frozen=True, slots=True)
class ValidateFunction:
    """One function the workflow registers, invokes, and inspects.

    No `key`: the legacy request carried one only to build task ids by hand
    (`images.build.{key}`). Sonata derives identity from the title, so the name
    is enough.
    """

    name: str
    image: str
    payload: str
    build_argv: tuple[str, ...]
    resources: dict[str, Any] | None = None
    scaling_config: dict[str, Any] | None = None
    timeout_ms: int = 5000
    concurrency: int = 2
    queue_size: int = 20
    max_retries: int = 3

    def manifest(self) -> FunctionManifest:
        return FunctionManifest(
            name=self.name,
            image=self.image,
            timeout_ms=self.timeout_ms,
            concurrency=self.concurrency,
            queue_size=self.queue_size,
            max_retries=self.max_retries,
            resources=self.resources,
            scaling_config=self.scaling_config,
        )


@dataclass(frozen=True, slots=True)
class ValidateWorkflowRequest:
    """Everything the validate workflow needs that is not an executor."""

    backend: Backend
    functions: tuple[ValidateFunction, ...]
    build: Build = "docker"
    namespace: str = "nanofaas-e2e"
    registry: str = "localhost:5000"
    additional_modules: tuple[str, ...] = ()
    build_images: bool = True
    control_plane_image: str | None = None
    helm_release: str = "nanofaas"
    helm_chart: str = "deploy/helm/nanofaas"
    helm_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.functions:
            raise ValueError("validate workflow requires at least one function")
        if not self.build_images and self.control_plane_image is None:
            raise ValueError("control_plane_image is required when build_images is false")

    @property
    def role(self) -> ExecutionRole:
        """Kubernetes work happens on the VM holding the cluster; container work here."""
        return "stack" if self.backend == "k8s" else "host"

    def control_plane_image_reference(self) -> str:
        return self.control_plane_image or f"{self.registry}/nanofaas/control-plane:e2e"


def _control_plane_build(
    request: ValidateWorkflowRequest,
    executor: CommandTaskExecutor,
    cwd: Path | None,
) -> GradleTask:
    target = (
        ":control-plane:bootBuildImage"
        if request.build == "buildpack"
        else ":control-plane:bootJar"
    )
    modules = (_MODULES[request.backend], *request.additional_modules)
    return GradleTask(
        target,
        title="Build control plane",
        executor=executor,
        role=request.role,
        properties={"controlPlaneModules": ",".join(modules)},
        cwd=cwd,
    )


def _helm_release_with_endpoint(
    request: ValidateWorkflowRequest,
    executor: CommandTaskExecutor,
    cwd: Path | None,
    requires: tuple[Resource[Any], ...],
) -> Resource[str]:
    """The control plane's Helm release, whose value is where it answers.

    A `Steps` composite rather than `helm_release_resource`, because this
    release's value is not its spec: the address exists only once the Service
    does. Three steps, each doing one thing and handing on what it produced —
    install, read the ClusterIP, say where that is.

    Still one compiled unit, so ordinals and `Selection` are unaffected; the
    steps show up in the event stream, where a run that used to sit silent
    through a chart install now says which half it is in.

    Resolving inside the acquire is what makes a chart that installs but never
    exposes a ClusterIP fail the acquire — and therefore uninstall — instead of
    leaving a release behind for a later task to trip over.
    """
    spec = HelmReleaseSpec(
        release=request.helm_release,
        chart=request.helm_chart,
        namespace=request.namespace,
        values=request.helm_values,
        role=request.role,
    )
    uninstall = HelmUninstallTask(spec, executor=executor, cwd=cwd)
    acquire = Steps(
        title=f"Acquire Helm release {spec.release}",
        steps=(
            HelmInstallTask(spec, executor=executor, cwd=cwd),
            KubectlTask(
                "get",
                "service",
                CONTROL_PLANE_SERVICE,
                "-o=jsonpath={.spec.clusterIP}",
                executor=executor,
                role=request.role,
                namespace=request.namespace,
                title=f"Read the {CONTROL_PLANE_SERVICE} address",
                cwd=cwd,
            ),
            ClusterIpEndpointTask(service=CONTROL_PLANE_SERVICE, port=CONTROL_PLANE_PORT),
        ),
    )

    return compensated_resource(
        title=f"Acquire Helm release {spec.release}",
        acquire=lambda inputs: cast(str, acquire.run(inputs).value),
        compensate=uninstall.run,
        requires=requires,
    )


def _inspection_task(
    request: ValidateWorkflowRequest,
    function: ValidateFunction,
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

    if request.backend == "k8s":
        workflow.add(
            KubectlTask(
                "version",
                "--client",
                executor=executor,
                role=request.role,
                title="Check kubectl is usable",
                cwd=cwd,
            )
        )

    if request.build_images:
        workflow.add(_control_plane_build(request, executor, cwd))
        if request.backend == "k8s":
            image = request.control_plane_image_reference()
            workflow.add(
                DockerBuildTask(
                    image=image,
                    dockerfile="platform/control-plane/Dockerfile",
                    context="platform/control-plane",
                    executor=executor,
                    role=request.role,
                    cwd=cwd,
                )
            )
            workflow.add(
                DockerPushTask(image=image, executor=executor, role=request.role, cwd=cwd)
            )
        for function in request.functions:
            workflow.add(
                CommandTask(
                    title=f"Build image {function.name}",
                    argv=function.build_argv,
                    executor=executor,
                    role=request.role,
                    cwd=cwd,
                )
            )
            if request.backend == "k8s":
                workflow.add(
                    DockerPushTask(
                        image=function.image, executor=executor, role=request.role, cwd=cwd
                    )
                )

    platform: tuple[Resource[Any], ...] = ()
    endpoint: Endpoint = local_endpoint
    if request.backend == "k8s":
        release = _helm_release_with_endpoint(request, executor, cwd, requires)
        platform, endpoint = (release,), release
    elif control_plane_process is not None:
        platform = (control_plane_process(),)

    for function in request.functions:
        manifest = function.manifest()
        registered = function_resource(
            name=function.name,
            register=HttpFunctionRegisterTask(
                manifest,
                endpoint=endpoint,
                executor=executor,
                role=request.role,
                cwd=cwd,
            ),
            delete=HttpFunctionDeleteTask(
                function.name,
                endpoint=endpoint,
                executor=executor,
                role=request.role,
                cwd=cwd,
            ),
            requires=(*requires, *platform),
        )
        workflow.add(
            HttpFunctionInvokeTask(
                function.name,
                payload=function.payload,
                endpoint=endpoint,
                executor=executor,
                role=request.role,
                cwd=cwd,
            ),
            requires=(*requires, *platform, registered),
        )
        workflow.add(
            _inspection_task(request, function, executor, cwd),
            requires=(*requires, registered),
        )
    return workflow
