import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from multipass import find_ssh_public_key
from sonata_engine import Resource, TaskInputs, Workflow
from sonata_tasks.cli import (
    RUNTIME_CONFIG_NAMESPACE,
    CliFunction,
    CliWorkflowRequest,
    build_cli_workflow,
)
from sonata_tasks.command import CommandTask
from sonata_tasks.deployment import CONTROL_PLANE_NODE_PORT, DEFAULT_NAMESPACE, LOCAL_REGISTRY
from sonata_tasks.helm import HelmReleaseSpec, helm_release_resource
from sonata_tasks.process import managed_process_resource
from sonata_tasks.registry import docker_registry_resource
from sonata_tasks.provisioning.resources import provisioned_vm
from sonata_tasks.components.bootstrap import (
    plan_k3s_install,
    plan_repo_sync_to_vm,
    plan_vm_provision_base,
    remote_project_dir,
    retarget_bootstrap_operation,
)
from sonata_tasks.components.context import ScenarioExecutionContext
from sonata_tasks.components.helm import control_plane_helm_values, helm_set_args
from sonata_tasks.components.images import control_image
from sonata_tasks.components.operations import RemoteCommandOperation, ScenarioOperation
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.vm.models import VmInfo, VmRequest
from sonata_tasks.vm.multipass import _find_ssh_private_key_path, repo_sync_ssh_rsh

from nanolab.cli.vm_provider import provider_for_environment, vm_request_for_role
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans import _local_control_plane
from nanolab.plans.functions import resolve_function
from nanolab.release.publish import GHCR_REPOSITORY
from nanolab.release.environment import secure_release_endpoints
from nanolab.release.versioning import normalize_version, read_project_version

LOCAL_ENDPOINT = _local_control_plane.ENDPOINT
# The runtime-config module carries both the admin API `control-plane config`
# drives and the `control-plane` namespace it patches, so nothing else has to be
# built in for the check to have something to talk to.
LOCAL_CONTROL_PLANE_BUILD_ARGV = (
    "./gradlew",
    ":control-plane:bootJar",
    "-PcontrolPlaneModules=container-deployment-provider,runtime-config",
    "--no-daemon",
)

# The provisioned k8s path runs the CLI *inside* the VM: the Helm chart exposes
# the control plane as a NodePort, so the CLI never needs to discover an
# external URL or fight a cloud provider's firewall.
PROVISIONED_ENDPOINT = f"http://127.0.0.1:{CONTROL_PLANE_NODE_PORT}"

# Nothing in this workflow reads a local registry: images come from GHCR, and the
# two bootstrap planners that consumed this value are not reused (see
# `_BOOTSTRAP_STEPS`). It survives only because `ScenarioExecutionContext` requires
# the field, and because `control_image()` is the canonical source of the image's
# name — `_published_image` keeps the name and discards this prefix.
_LOCAL_REGISTRY = LOCAL_REGISTRY
_HELM_CHART = "deploy/helm/nanofaas"
_HELM_RELEASE = "control-plane"
_FUNCTION_READINESS_TIMEOUT_SECONDS = 120

# The CLI is built on the host and run inside the VM, and the repository sync
# obeys nanoFaaS' .gitignore, which excludes every build directory — so the
# binary this workflow exists to exercise is the one thing the sync cannot
# carry. It gets its own step.
_CLI_INSTALL_DIR = "clients/cli/build/install"

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


def _local_control_plane_resource(
    repo_root: Path, *, requires: tuple[Resource[Any], ...] = ()
) -> Resource:
    """The control plane running on this machine, deploying functions with Docker."""
    return replace(
        managed_process_resource(
            title="Acquire local control plane",
            argv=_local_control_plane.argv(repo_root, admin_runtime_config=True),
            cwd=repo_root,
            ready=_local_control_plane.ready,
        ),
        requires=requires,
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
    resolved_context = cast(ScenarioExecutionContext, replace(context, vm_request=resolved_request))
    return resolved_context


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

    def resolve_cli_sync_argv(inputs: TaskInputs) -> tuple[str, ...]:
        info = inputs.resource(vm)
        destination = remote_project_dir(
            vm_request.model_copy(update={"user": info.user, "home": info.home})
        )
        # `--relative` with the `/./` marker recreates clients/cli/build/install
        # under the destination, so nothing has to mkdir -p ahead of it.
        return (
            "rsync",
            "-az",
            "--delete",
            "--relative",
            "-e",
            repo_sync_ssh_rsh(resolve_private_key()),
            f"{repo_root}/./{_CLI_INSTALL_DIR}/",
            f"{info.user}@{info.host}:{destination}/",
        )

    tasks.append(
        CommandTask(
            title="Sync nanofaas CLI into VM",
            argv=resolve_cli_sync_argv,
            executor=executor,
            role="host",
        )
    )
    return tuple(tasks)


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
        # The released image carries every module, so the runtime-config admin API
        # is one property away; the CLI's `control-plane config` commands are
        # unreachable without it.
        admin_runtime_config=True,
    )
    spec = HelmReleaseSpec(
        release=_HELM_RELEASE,
        chart=_HELM_CHART,
        namespace=namespace,
        values=helm_set_args(values),
    )
    resource = helm_release_resource(spec, executor=executor, requires=(vm,))
    # Retitled so the compiled id reads "acquire-control-plane-helm-release":
    # `helm_release_resource`'s own title is generic ("Acquire Helm release
    # <name>"), but this workflow's topology names the release by what it's for.
    return replace(resource, title="Acquire control-plane Helm release")


def _stack_vm_request(environment: EnvironmentConfig) -> VmRequest:
    if environment.provider == "local":
        raise ValueError("a non-local environment is required")
    return vm_request_for_role(environment, "stack", loadtest=False)


def _build_k8s_plan(
    config: ScenarioConfig,
    bindings: RoleBindings,
    *,
    namespace: str,
    repo_root: Path,
    environment: EnvironmentConfig | None,
    orchestrator_factory: Callable[[Path], Any] | None,
) -> Workflow:
    if environment is None:
        raise ValueError("k8s cli workflow requires an environment")
    vm_request = _stack_vm_request(environment)
    orchestrator = provider_for_environment(
        environment, repo_root, orchestrator_factory=orchestrator_factory
    )
    def after_ensure(_info: VmInfo) -> None:
        if (
            environment.provider == "azure"
            and environment.azure is not None
            and environment.azure.operator_source_cidr
        ):
            secure_release_endpoints(environment, orchestrator, vm_request, None)

    vm = provisioned_vm(
        title="Acquire stack VM",
        request=vm_request,
        provider=orchestrator,
        after_ensure=after_ensure,
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
        for resolved in (resolve_function(config, key, source_root=repo_root),)
    )
    request = CliWorkflowRequest(
        functions=functions,
        cli_role="stack",
        build_role="host",
        endpoint=PROVISIONED_ENDPOINT,
        namespace=namespace,
        runtime_config_namespace=RUNTIME_CONFIG_NAMESPACE,
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


def build_cli_plan(  # NOSONAR (S3776): selects one complete deployment graph
    config: ScenarioConfig,
    bindings: RoleBindings,
    *,
    cli_role: ExecutionRole = "host",
    endpoint: str | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    repo_root: Path | None = None,
    environment: EnvironmentConfig | None = None,
    orchestrator_factory: Callable[[Path], Any] | None = None,
) -> Workflow:
    if config.workflow != "cli":
        raise ValueError("CLI plan requires a cli scenario")
    root = repo_root or Path.cwd()
    local = config.backend == "container"
    if local and cli_role != "host":
        raise ValueError("container cli workflow must run on the host role")
    if not local and environment is not None and environment.provider != "local":
        return _build_k8s_plan(
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
        for resolved in (resolve_function(config, key, source_root=repo_root),)
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
        push_function_images=local,
        # Only the control plane this plan starts itself is known to expose a
        # runtime-config namespace; an endpoint someone passed in is not.
        runtime_config_namespace=RUNTIME_CONFIG_NAMESPACE if local else None,
    )
    registry = (
        docker_registry_resource(
            executor=RoleBoundCommandTaskExecutor(bindings),
            role="host",
        )
        if local
        else None
    )
    requires = (
        (registry, _local_control_plane_resource(root, requires=(registry,)))
        if registry is not None
        else ()
    )
    return build_cli_workflow(
        request,
        bindings,
        cwd=root,
        control_plane_build_argv=LOCAL_CONTROL_PLANE_BUILD_ARGV if local else None,
        requires=requires,
        push_requires=(registry,) if registry is not None else (),
    )
