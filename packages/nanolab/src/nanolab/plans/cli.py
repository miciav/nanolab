import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from multipass import find_ssh_public_key
from sonata_engine import Resource, TaskInputs, Workflow
from sonata_tasks.cli import CliFunction, CliWorkflowRequest, build_cli_workflow
from sonata_tasks.command import CommandTask
from sonata_tasks.helm import HelmReleaseSpec, helm_release_resource
from sonata_tasks.process import managed_process_resource
from sonata_tasks.vm import vm_resource
from workflow_tasks.components.bootstrap import (
    plan_k3s_install,
    plan_repo_sync_to_vm,
    plan_vm_provision_base,
    retarget_bootstrap_operation,
)
from workflow_tasks.components.context import ScenarioExecutionContext
from workflow_tasks.components.helm import control_plane_helm_values
from workflow_tasks.components.images import control_image
from workflow_tasks.components.operations import RemoteCommandOperation, ScenarioOperation
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.vm.adapters import VmLifecycleAdapter
from sonata_tasks.vm.models import VmConfig, VmInfo, VmRequest, vm_remote_home
from workflow_tasks.vm.multipass import _find_ssh_private_key_path
from workflow_tasks.vm.orchestrator import VmOrchestrator

from nanolab.cli.vm_provider import vm_provider_for_environment, vm_request_for_role
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans import _local_control_plane
from nanolab.plans.validate import _resolve_function
from nanolab.release.publish import GHCR_REPOSITORY
from nanolab.release.versioning import normalize_version, read_project_version

LOCAL_ENDPOINT = _local_control_plane.ENDPOINT
LOCAL_CONTROL_PLANE_BUILD_ARGV = (
    "./gradlew",
    ":control-plane:bootJar",
    "-PcontrolPlaneModules=container-deployment-provider",
    "--no-daemon",
)

# The provisioned k8s path runs the CLI *inside* the VM: the Helm chart exposes
# the control plane as a NodePort, so the CLI never needs to discover an
# external URL or fight a cloud provider's firewall.
PROVISIONED_ENDPOINT = "http://127.0.0.1:30080"

# Nothing in this workflow reads a local registry: images come from GHCR, and the
# two bootstrap planners that consumed this value are not reused (see
# `_BOOTSTRAP_STEPS`). It survives only because `ScenarioExecutionContext` requires
# the field, and because `control_image()` is the canonical source of the image's
# name — `_published_image` keeps the name and discards this prefix.
_LOCAL_REGISTRY = "localhost:5000"
_HELM_CHART = "deploy/helm/nanofaas"
_HELM_RELEASE = "control-plane"
_FUNCTION_READINESS_TIMEOUT_SECONDS = 120

# (task title, planner) — the bootstrap sequence the CLI-provisioned workflow
# reuses from the legacy provisioning components. Titles are chosen so their
# compiled slug matches the plan's expected topology; they are not always the
# planner's own `summary`.
#
# The legacy sequence also starts a local registry and points k3s at it, because
# `validate` builds its images on the spot and pushes them there. This workflow
# pulls published images from GHCR instead, so both of those steps would set up
# a registry nothing ever reads. They are deliberately not reused.
_BOOTSTRAP_STEPS: tuple[
    tuple[str, Callable[..., tuple[ScenarioOperation, ...]]], ...
] = (
    ("Provision base VM dependencies", plan_vm_provision_base),
    ("Install k3s", plan_k3s_install),
    ("Sync repository into VM", plan_repo_sync_to_vm),
)


def _local_control_plane_resource(repo_root: Path) -> Resource:
    """The control plane running on this machine, deploying functions with Docker."""
    return managed_process_resource(
        title="Acquire local control plane",
        argv=_local_control_plane.argv(repo_root),
        cwd=repo_root,
        ready=_local_control_plane.ready,
    )


def _placeholder_context(repo_root: Path, vm_request: VmRequest) -> ScenarioExecutionContext:
    """A context good enough to plan the bootstrap operations before any VM exists.

    Only `vm_request.host` is genuinely unknown at this point; the ansible/rsync
    planners only read `user`/`home`, which are already fixed by the request.
    """
    return ScenarioExecutionContext(
        repo_root=repo_root,
        scenario_name="cli-provision",
        runtime="java",
        namespace=None,
        local_registry=_LOCAL_REGISTRY,
        resolved_scenario=None,
        vm_request=vm_request,
        cleanup_vm=False,
    )


def _resolved_context(
    context: ScenarioExecutionContext, info: VmInfo
) -> ScenarioExecutionContext:
    resolved_request = context.vm_request.model_copy(
        update={"lifecycle": "external", "host": info.host, "user": info.user, "home": info.home}
    )
    return replace(context, vm_request=resolved_request)


def _bootstrap_tasks(
    *,
    repo_root: Path,
    vm_request: VmRequest,
    vm: Resource[VmInfo],
    executor: RoleBoundCommandTaskExecutor,
) -> tuple[CommandTask, ...]:
    """The 5 provisioning steps as Sonata tasks, retargeted at the resolved VM.

    Each planner runs once, eagerly, against a placeholder context (no VM is up
    yet). The resulting operation's argv is then re-resolved at run time — once
    the VM resource has produced a real `VmInfo` — via `retarget_bootstrap_operation`,
    the same helper the legacy cloud-provider path already uses.
    """
    placeholder_context = _placeholder_context(repo_root, vm_request)
    private_key: Path | None = None
    private_key_resolved = False

    def resolve_private_key() -> Path | None:
        nonlocal private_key, private_key_resolved
        if not private_key_resolved:
            private_key = _find_ssh_private_key_path(find_ssh_public_key())
            private_key_resolved = True
        return private_key

    tasks: list[CommandTask] = []
    for title, planner in _BOOTSTRAP_STEPS:
        (raw_operation,) = planner(placeholder_context, discover_private_key=False)
        if not isinstance(raw_operation, RemoteCommandOperation):
            raise TypeError(f"bootstrap operation is not a remote command: {raw_operation}")
        base_operation = raw_operation

        def resolve_argv(
            inputs: TaskInputs, *, base_operation: RemoteCommandOperation = base_operation
        ) -> tuple[str, ...]:
            info = inputs.resource(vm)
            retargeted = retarget_bootstrap_operation(
                base_operation,
                context=_resolved_context(placeholder_context, info),
                host=info.host,
                private_key=resolve_private_key(),
            )
            return retargeted.argv

        tasks.append(
            CommandTask(
                title=title,
                argv=resolve_argv,
                executor=executor,
                role="host",
                env=base_operation.env,
            )
        )
    return tuple(tasks)


def _set_args(values: dict[str, str]) -> tuple[str, ...]:
    args: list[str] = []
    for key, value in values.items():
        args.extend(["--set", f"{key}={value}"])
    return tuple(args)


def _published_image(image: str, version_tag: str) -> str:
    target = image.rsplit("/", 1)[-1].split(":", 1)[0]
    return f"{GHCR_REPOSITORY}/{target}:{version_tag}"


def _control_plane_helm_resource(
    *,
    namespace: str,
    control_plane_image: str,
    executor: RoleBoundCommandTaskExecutor,
    vm: Resource[VmInfo],
) -> Resource[HelmReleaseSpec]:
    """The control plane Helm release, deployed inside the VM.

    This workflow verifies the CLI end-to-end, not the control plane build: it
    uses the official image release matching the product checkout. A freshly
    provisioned VM's local registry is empty, and this workflow deliberately
    has no image build/push phase.
    """
    values = control_plane_helm_values(
        namespace=namespace,
        control_plane_image=control_plane_image,
        expose_node_port=True,
    )
    spec = HelmReleaseSpec(
        release=_HELM_RELEASE,
        chart=_HELM_CHART,
        namespace=namespace,
        values=_set_args(values),
    )
    resource = helm_release_resource(spec, executor=executor, requires=(vm,))
    # Retitled so the compiled id reads "acquire-control-plane-helm-release":
    # `helm_release_resource`'s own title is generic ("Acquire Helm release
    # <name>"), but this workflow's topology names the release by what it's for.
    return replace(resource, title="Acquire control-plane Helm release")


def _stack_vm_request(environment: EnvironmentConfig) -> VmRequest:
    if environment.provider == "local":
        raise ValueError("--provision requires a non-local environment")
    return vm_request_for_role(environment, "stack", loadtest=False)


def _stack_orchestrator(
    environment: EnvironmentConfig,
    repo_root: Path,
    orchestrator_factory: Callable[[Path], Any] | None,
) -> Any:
    if orchestrator_factory is not None:
        return orchestrator_factory(repo_root)
    if environment.provider in ("azure", "proxmox"):
        return vm_provider_for_environment(environment, repo_root)
    return VmOrchestrator(repo_root)


def _build_provisioned_k8s_plan(
    config: ScenarioConfig,
    bindings: RoleBindings,
    *,
    namespace: str,
    repo_root: Path,
    environment: EnvironmentConfig | None,
    orchestrator_factory: Callable[[Path], Any] | None,
) -> Workflow:
    if environment is None:
        raise ValueError("--provision requires an environment")
    vm_request = _stack_vm_request(environment)
    orchestrator = _stack_orchestrator(environment, repo_root, orchestrator_factory)
    lifecycle = VmLifecycleAdapter(orchestrator, lifecycle=vm_request.lifecycle, credentials=vm_request)
    vm_config = VmConfig(
        name=vm_request.name or "nanofaas-e2e",
        cpus=vm_request.cpus,
        memory=vm_request.memory,
        disk=vm_request.disk,
    )
    fallback_info = VmInfo(
        name=vm_config.name,
        host=vm_request.host or "",
        user=vm_request.user,
        home=vm_remote_home(vm_request),
    )
    vm = vm_resource(
        title="Acquire stack VM",
        lifecycle=lifecycle,
        config=vm_config,
        fallback_info=fallback_info,
        external=environment.provider == "external",
    )

    executor = RoleBoundCommandTaskExecutor(bindings)
    bootstrap = _bootstrap_tasks(
        repo_root=repo_root, vm_request=vm_request, vm=vm, executor=executor
    )
    _, version_tag = normalize_version(read_project_version(repo_root))
    helm = _control_plane_helm_resource(
        namespace=namespace,
        control_plane_image=_published_image(control_image(_LOCAL_REGISTRY), version_tag),
        executor=executor,
        vm=vm,
    )

    functions = tuple(
        CliFunction(
            name=resolved.name,
            image=_published_image(resolved.image, version_tag),
            payload=json.dumps(json.loads(resolved.payload)["input"], separators=(",", ":")),
            resources=resolved.resources,
            # Function images use the same published release as the control
            # plane; this workflow deliberately has no image build/push phase.
            build_argv=None,
        )
        for key in config.functions
        for resolved in (_resolve_function(config, key),)
    )
    request = CliWorkflowRequest(
        functions=functions,
        cli_role="stack",
        build_role="host",
        endpoint=PROVISIONED_ENDPOINT,
        namespace=namespace,
    )
    return build_cli_workflow(
        request,
        bindings,
        cwd=repo_root,
        bootstrap=bootstrap,
        bootstrap_requires=(vm,),
        function_requires=(helm,),
        readiness_timeout_seconds=_FUNCTION_READINESS_TIMEOUT_SECONDS,
    )


def build_cli_plan(
    config: ScenarioConfig,
    bindings: RoleBindings,
    *,
    cli_role: ExecutionRole = "host",
    endpoint: str | None = None,
    namespace: str = "nanofaas-e2e",
    repo_root: Path | None = None,
    provision: bool = False,
    environment: EnvironmentConfig | None = None,
    orchestrator_factory: Callable[[Path], Any] | None = None,
) -> Workflow:
    if config.workflow != "cli":
        raise ValueError("CLI plan requires a cli scenario")
    root = repo_root or Path.cwd()
    local = config.backend == "container"
    if local and cli_role != "host":
        raise ValueError("container cli workflow must run on the host role")
    if local and provision:
        raise ValueError("container cli workflow does not support --provision")
    if provision:
        return _build_provisioned_k8s_plan(
            config,
            bindings,
            namespace=namespace,
            repo_root=root,
            environment=environment,
            orchestrator_factory=orchestrator_factory,
        )
    if not local and endpoint is None:
        raise ValueError("k8s cli workflow requires an explicit control-plane URL")
    target_endpoint = LOCAL_ENDPOINT if local else endpoint
    assert target_endpoint is not None
    functions = tuple(
        CliFunction(
            name=resolved.name,
            image=resolved.image,
            payload=json.dumps(json.loads(resolved.payload)["input"], separators=(",", ":")),
            resources=resolved.resources,
            build_argv=resolved.build_argv if local else None,
            image_build_argv=resolved.image_build_argv if local else None,
        )
        for key in config.functions
        for resolved in (_resolve_function(config, key),)
    )
    request = CliWorkflowRequest(
        functions=functions,
        cli_role=cli_role,
        # Build and CLI share a role here, same as before `build_role` existed:
        # only the provisioned k8s path (built on the host, run inside the VM)
        # needs them to differ.
        build_role=cli_role,
        endpoint=target_endpoint,
        namespace=namespace,
    )
    requires = (_local_control_plane_resource(root),) if local else ()
    return build_cli_workflow(
        request,
        bindings,
        cwd=root,
        control_plane_build_argv=LOCAL_CONTROL_PLANE_BUILD_ARGV if local else None,
        requires=requires,
    )
