from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Literal, cast

from sonata_engine import Resource, Steps, Workflow
from sonata_tasks.execution.bindings import (
    CommandTaskExecutor,
)
from sonata_tasks.execution.roles import ExecutionRole

from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.deployment import (
    DEFAULT_NAMESPACE,
    LOCAL_CONTROL_PLANE_API_PORT,
    LOCAL_REGISTRY,
)
from sonata_tasks.docker import DockerBuildTask, DockerPushTask
from sonata_tasks.function import function_resource
from sonata_tasks.gradle import GradleTask
from sonata_tasks.helm import HelmInstallTask, HelmReleaseSpec, HelmUninstallTask
from sonata_tasks.http_function import (
    Endpoint,
    HttpFunctionDeleteTask,
    HttpFunctionRegisterTask,
)
from sonata_tasks.kubectl import (
    ClusterIpEndpointTask,
    KubectlTask,
    k8s_deployment_readiness,
)
from sonata_tasks.manifest import FunctionManifest

Backend = Literal["container", "k8s"]
Build = Literal["docker", "buildpack"]

CONTROL_PLANE_SERVICE = "control-plane"
CONTROL_PLANE_PORT = 8080

_MODULES: dict[Backend, str] = {
    "container": "container-deployment-provider",
    "k8s": "k8s-deployment-provider",
}


@dataclass(frozen=True, slots=True)
class PlatformFunction:
    """One function the platform is asked to deploy.

    No `key`: the legacy request carried one only to build task ids by hand
    (`images.build.{key}`). Sonata derives identity from the title, so the name
    is enough.
    """

    name: str
    image: str
    payload: str
    build_argv: tuple[str, ...]
    image_build_argv: tuple[str, ...] | None = None
    resources: dict[str, Any] | None = None
    scaling_config: dict[str, Any] | None = None
    offload: dict[str, Any] | None = None
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
            offload=self.offload,
        )


@dataclass(frozen=True, slots=True)
class PlatformRequest:
    """Everything the shared platform half needs that is not an executor."""

    backend: Backend
    functions: tuple[PlatformFunction, ...]
    build: Build = "docker"
    namespace: str = DEFAULT_NAMESPACE
    registry: str = LOCAL_REGISTRY
    additional_modules: tuple[str, ...] = ()
    build_images: bool = True
    build_control_plane: bool = True
    push_function_images: bool = False
    control_plane_image: str | None = None
    # What the source being built looks like. Callers that can fingerprint their
    # checkout pass it here and the built image is named after it, so a rebuilt
    # control plane arrives under a name the cluster has never seen and Helm
    # rolls the pod. A fixed tag leaves the manifest identical, and the old pod
    # keeps running while every task reports success.
    source_fingerprint: str | None = None
    helm_release: str = "nanofaas"
    helm_chart: str = "deploy/helm/nanofaas"
    helm_values: tuple[str, ...] = ()
    # Overrides the role the backend implies. `offload-loadtest` puts two
    # platforms on two clusters at once, so one of them cannot be "stack".
    execution_role: ExecutionRole | None = None
    # Names the side in every title this platform contributes. Empty for the
    # workflows with one platform; without it a plan holding two shows
    # "Check kubectl is usable" twice and a reader cannot tell which cluster.
    label: str = ""

    def titled(self, title: str) -> str:
        return f"{title} on the {self.label}" if self.label else title

    def __post_init__(self) -> None:
        if not self.functions:
            raise ValueError("a platform request needs at least one function")
        if (
            self.backend == "k8s"
            and not self.build_images
            and self.control_plane_image is None
        ):
            raise ValueError("control_plane_image is required when build_images is false")

    @property
    def role(self) -> ExecutionRole:
        """Kubernetes work happens on the VM holding the cluster; container work here."""
        if self.execution_role is not None:
            return self.execution_role
        return "stack" if self.backend == "k8s" else "host"

    def control_plane_modules(self) -> tuple[str, ...]:
        return (_MODULES[self.backend], *self.additional_modules)

    def control_plane_image_tag(self) -> str:
        """The tag the built control plane is published under.

        The modules go into the hash because they are a build input: the same
        checkout compiled with and without the autoscaler yields two different
        binaries, and two scenarios that differ only there would otherwise push
        both under one name — the second one silently never reaching the cluster.
        """
        if self.source_fingerprint is None:
            return "e2e"
        digest = hashlib.sha256(
            "\0".join((self.source_fingerprint, *self.control_plane_modules())).encode("utf-8")
        ).hexdigest()
        return f"e2e-{digest[:12]}"

    def control_plane_image_reference(self) -> str:
        return (
            self.control_plane_image
            or f"{self.registry}/nanofaas/control-plane:{self.control_plane_image_tag()}"
        )


def _control_plane_build(
    request: PlatformRequest,
    executor: CommandTaskExecutor,
    cwd: Path | None,
) -> GradleTask:
    target = ":control-plane:bootJar"
    modules = request.control_plane_modules()
    return GradleTask(
        target,
        title=request.titled("Build control plane"),
        executor=executor,
        role=request.role,
        properties={"controlPlaneModules": ",".join(modules)},
        cwd=cwd,
    )


def _helm_release_with_endpoint(
    request: PlatformRequest,
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
        title=request.titled(f"Acquire Helm release {spec.release}"),
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
        title=request.titled(f"Acquire Helm release {spec.release}"),
        acquire=lambda inputs: cast(str, acquire.run(inputs).value),
        compensate=uninstall.run,
        requires=requires,
    )



@dataclass(frozen=True, slots=True)
class Platform:
    """What the platform half left behind for a workflow to use.

    `endpoint` is where the control plane answers — a plain string locally, the
    Helm release resource on Kubernetes, where the address exists only once the
    Service does. `functions` are the registered functions, one resource each,
    so a consumer declares the ones it needs and the compiler places their
    release after the last of them.
    """

    endpoint: Endpoint
    resources: tuple[Resource[Any], ...]
    functions: tuple[Resource[None], ...]


def _check_kubectl(
    workflow: Workflow,
    request: PlatformRequest,
    executor: CommandTaskExecutor,
    cwd: Path | None,
    requires: tuple[Resource[Any], ...],
) -> None:
    if request.backend == "k8s":
        workflow.add(
            KubectlTask(
                "version",
                "--client",
                executor=executor,
                role=request.role,
                title=request.titled("Check kubectl is usable"),
                cwd=cwd,
            ),
            requires=requires,
        )


def _add_function_build(
    workflow: Workflow,
    request: PlatformRequest,
    function: PlatformFunction,
    executor: CommandTaskExecutor,
    cwd: Path | None,
    requires: tuple[Resource[Any], ...],
) -> None:
    if function.image_build_argv is not None:
        workflow.add(
            CommandTask(
                title=request.titled(f"Build application artifact: {function.name}"),
                argv=function.build_argv,
                executor=executor,
                role=request.role,
                cwd=cwd,
            ),
            requires=requires,
        )
    workflow.add(
        CommandTask(
            title=request.titled(f"Build image {function.name}"),
            argv=function.image_build_argv or function.build_argv,
            executor=executor,
            role=request.role,
            cwd=cwd,
        ),
        requires=requires,
    )
    if request.backend == "k8s" or request.push_function_images:
        workflow.add(
            DockerPushTask(
                image=function.image,
                executor=executor,
                role=request.role,
                title=request.titled(f"Push image {function.image}"),
                cwd=cwd,
            ),
            requires=requires,
        )


def _add_build_tasks(
    workflow: Workflow,
    request: PlatformRequest,
    executor: CommandTaskExecutor,
    cwd: Path | None,
    requires: tuple[Resource[Any], ...],
) -> None:
    if request.build_images:
        if request.build_control_plane:
            workflow.add(
                _control_plane_build(request, executor, cwd),
                requires=requires,
            )
        if request.backend == "k8s":
            image = request.control_plane_image_reference()
            # Titled by repository, not by the full reference: the tag now
            # carries a source fingerprint, and a task id that changes with
            # every edit would churn the journal and every run's task list.
            repository = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
            workflow.add(
                DockerBuildTask(
                    image=image,
                    dockerfile="platform/control-plane/Dockerfile",
                    context="platform/control-plane",
                    executor=executor,
                    role=request.role,
                    title=request.titled(f"Build image {repository}"),
                    cwd=cwd,
                ),
                requires=requires,
            )
            workflow.add(
                DockerPushTask(
                    image=image,
                    executor=executor,
                    role=request.role,
                    title=request.titled(f"Push image {repository}"),
                    cwd=cwd,
                ),
                requires=requires,
            )
        for function in request.functions:
            _add_function_build(workflow, request, function, executor, cwd, requires)


def _resolve_platform_endpoint(
    request: PlatformRequest,
    control_plane_process: Callable[[], Resource[Any]] | None,
    executor: CommandTaskExecutor,
    cwd: Path | None,
    requires: tuple[Resource[Any], ...],
    local_endpoint: str,
) -> tuple[tuple[Resource[Any], ...], Endpoint]:
    resources: tuple[Resource[Any], ...] = ()
    endpoint: Endpoint = local_endpoint
    if request.backend == "k8s":
        release = _helm_release_with_endpoint(request, executor, cwd, requires)
        resources, endpoint = (release,), release
    elif control_plane_process is not None:
        resources = (control_plane_process(),)
    return resources, endpoint


def _function_resource(
    request: PlatformRequest,
    function: PlatformFunction,
    endpoint: Endpoint,
    requires: tuple[Resource[Any], ...],
    resources: tuple[Resource[Any], ...],
    executor: CommandTaskExecutor,
    cwd: Path | None,
) -> Resource[None]:
    return function_resource(
        name=request.titled(function.name),
        # Registering only asks the control plane to create the Deployment;
        # it answers before the pod does. A live run invoked 0.4s later and
        # got POOL_ERROR: Connection refused against a Service whose
        # ClusterIP already existed.
        readiness=(
            k8s_deployment_readiness(
                deployment=f"fn-{function.name}",
                namespace=request.namespace,
                executor=executor,
                role=request.role,
                cwd=cwd,
            )
            if request.backend == "k8s"
            else ()
        ),
        register=HttpFunctionRegisterTask(
            function.manifest(),
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
        requires=(*requires, *resources),
    )


def add_platform(
    workflow: Workflow,
    request: PlatformRequest,
    *,
    executor: CommandTaskExecutor,
    cwd: Path | None = None,
    control_plane_process: Callable[[], Resource[Any]] | None = None,
    local_endpoint: str = f"http://127.0.0.1:{LOCAL_CONTROL_PLANE_API_PORT}",
    requires: tuple[Resource[Any], ...] = (),
) -> Platform:
    """Add everything up to and including registered functions.

    Shared because `validate` and `loadtest` need exactly the same platform and
    differ only in what they do to it afterwards — one invokes and inspects, the
    other puts it under load. The legacy pair had the same arrangement, with
    `loadtest` importing `k8s_deployment_specs` out of the validate module; this
    is that, given a home of its own so neither workflow imports the other.
    """
    _check_kubectl(workflow, request, executor, cwd, requires)

    _add_build_tasks(workflow, request, executor, cwd, requires)

    resources, endpoint = _resolve_platform_endpoint(
        request, control_plane_process, executor, cwd, requires, local_endpoint
    )

    functions = tuple(
        _function_resource(request, function, endpoint, requires, resources, executor, cwd)
        for function in request.functions
    )

    return Platform(endpoint=endpoint, resources=resources, functions=functions)
