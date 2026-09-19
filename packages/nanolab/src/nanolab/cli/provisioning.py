"""Provisioning: translate nanolab config onto sonata_tasks provisioning resources."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from sonata_tasks.vm.azure import AzureVmProvider
from sonata_tasks.vm.proxmox import ProxmoxVmProvider

from nanolab.cli.vm_provider import provider_for_environment, vm_request_for_role
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.environment import ExecutionRole
from nanolab.release.environment import secure_release_endpoints
from nanolab.tasks.components.bootstrap import (
    plan_assets_sync_to_vm,
    plan_containerd_maven_sync_to_vm,
    plan_containerd_rootless_install,
    plan_k3s_configure_registry,
    plan_k3s_install,
    plan_loadtest_install_k6,
    plan_registry_ensure_container,
    plan_repo_sync_to_vm,
    plan_vm_provision_base,
    retarget_bootstrap_operation,
)
from nanolab.tasks.components.context import ScenarioExecutionContext
from nanolab.tasks.components.operations import (
    RemoteCommandOperation,
    ScenarioOperation,
)
from nanolab.tasks.containerd_maven import (
    remote_repository_path,
    stage_snapshot_repository,
)
from nanolab.tasks.provisioning import (
    ProvisionedRole,
    provision_roles,
    remote_operations,
    scenario_context,
)
from nanolab.tasks.vm.models import VmRequest
from nanolab.workspace.paths import discover_tool_root


def _request(
    environment: EnvironmentConfig, role: ExecutionRole, *, loadtest: bool
) -> VmRequest:
    return vm_request_for_role(environment, role, loadtest=loadtest)


def _stack_operations(
    scenario: ScenarioConfig,
    context: ScenarioExecutionContext,
    *,
    dedicated_loadgen: bool,
    include_repo_sync: bool = True,
    maven_source: Path | None = None,
    maven_destination: Path | None = None,
) -> tuple[RemoteCommandOperation, ...]:
    if scenario.backend == "containerd" and context.vm_request.user == "root":
        raise ValueError("containerd backend requires an unprivileged VM user")
    planners: list[
        Callable[[ScenarioExecutionContext], tuple[ScenarioOperation, ...]]
    ] = [plan_vm_provision_base]
    if scenario.backend == "k8s" or (
        scenario.workflow in ("loadtest", "release")
        and scenario.backend != "containerd"
    ):
        planners.extend(
            [
                plan_k3s_install,
                plan_registry_ensure_container,
                plan_k3s_configure_registry,
            ]
        )
    if scenario.backend == "containerd":
        planners.extend([plan_assets_sync_to_vm, plan_containerd_rootless_install])
    if (scenario.workflow == "loadtest" and not dedicated_loadgen) or (
        scenario.workflow == "validate" and scenario.backend == "k8s"
    ):
        planners.append(plan_loadtest_install_k6)
        if scenario.backend != "containerd":
            planners.append(plan_assets_sync_to_vm)
    if include_repo_sync:
        planners.append(plan_repo_sync_to_vm)
    operations = tuple(
        operation for planner in planners for operation in planner(context)
    )
    if maven_source is not None and maven_destination is not None:
        operations += plan_containerd_maven_sync_to_vm(
            context, source=maven_source, destination=maven_destination
        )
    return remote_operations(operations)


def _role_requests_and_operations(
    provider: Any,
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    repo_root: Path,
    maven_source: Path | None = None,
) -> list[tuple[ExecutionRole, VmRequest, tuple[RemoteCommandOperation, ...]]]:
    """Build the per-role (role, request, operations) triples."""
    loadtest_workflow = scenario.workflow in ("loadtest", "offload-loadtest", "release")
    dedicated_loadgen = loadtest_workflow and "loadgen" in environment.roles
    dedicated_cloud = (
        scenario.workflow == "offload-loadtest" and "cloud" in environment.roles
    )
    dedicated_arm = "arm-builder" in environment.roles
    assets_root = discover_tool_root() / "assets"

    def context_for(request: VmRequest) -> ScenarioExecutionContext:
        # Plan against the resolved-equivalent request: after ensure the request
        # carries lifecycle "external" and the connection host, and the planners
        # build inventories and rsync targets from exactly those two fields.
        resolved = request.model_copy(
            update={
                "lifecycle": "external",
                "host": request.host or f"{request.name}.internal",
            }
        )
        return scenario_context(repo_root, resolved, assets_root)

    def retarget(
        context: ScenarioExecutionContext,
        operations: tuple[RemoteCommandOperation, ...],
    ) -> tuple[RemoteCommandOperation, ...]:
        if environment.provider not in {"azure", "proxmox"}:
            return operations
        if isinstance(provider, (AzureVmProvider, ProxmoxVmProvider)):
            # Real cloud providers re-retarget inside provision_roles, after
            # ensure, from the resolved request; their endpoint helpers need
            # the VM to exist, so they must not be called here.
            return operations
        request = context.vm_request
        if environment.provider == "proxmox":
            host, port = provider.ssh_endpoint(request)
        else:
            host, port = provider.connection_host(request), None
        private_key = provider.ssh_private_key_path(request)
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

    triples: list[
        tuple[ExecutionRole, VmRequest, tuple[RemoteCommandOperation, ...]]
    ] = []

    stack_request = _request(environment, "stack", loadtest=loadtest_workflow)
    stack_context = context_for(stack_request)
    triples.append(
        (
            "stack",
            stack_request,
            retarget(
                stack_context,
                _stack_operations(
                    scenario,
                    stack_context,
                    dedicated_loadgen=dedicated_loadgen,
                    include_repo_sync=scenario.workflow != "release",
                    maven_source=maven_source,
                    maven_destination=(
                        remote_repository_path(environment)
                        if maven_source is not None
                        else None
                    ),
                ),
            ),
        )
    )

    if dedicated_loadgen:
        loadgen_request = _request(environment, "loadgen", loadtest=True)
        loadgen_context = context_for(loadgen_request)
        triples.append(
            (
                "loadgen",
                loadgen_request,
                retarget(
                    loadgen_context,
                    remote_operations(
                        (
                            *plan_loadtest_install_k6(loadgen_context),
                            *plan_assets_sync_to_vm(loadgen_context),
                            *(
                                plan_repo_sync_to_vm(loadgen_context)
                                if scenario.workflow != "release"
                                else ()
                            ),
                        )
                    ),
                ),
            )
        )

    if dedicated_cloud:
        cloud_request = _request(environment, "cloud", loadtest=True)
        cloud_context = context_for(cloud_request)
        triples.append(
            (
                "cloud",
                cloud_request,
                retarget(
                    cloud_context,
                    _stack_operations(
                        scenario,
                        cloud_context,
                        dedicated_loadgen=True,
                    ),
                ),
            )
        )

    if dedicated_arm:
        arm_request = _request(environment, "arm-builder", loadtest=False)
        arm_context = context_for(arm_request)
        triples.append(
            (
                "arm-builder",
                arm_request,
                remote_operations(plan_vm_provision_base(arm_context)),
            )
        )

    return triples


@contextmanager
def provision_environment(
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    repo_root: Path,
    orchestrator_factory: Callable[[Path], Any] | None = None,
    post_ensure_verifier: Callable[[ExecutionRole, VmRequest], None] | None = None,
    keep: bool = False,
) -> Generator[None, None, None]:
    """Bring up every role the scenario needs, and tear them down on exit.

    Rejects a local environment, which has no machines to provision. On Azure
    with an operator CIDR, the stack's endpoints are bounded to it once the
    stack is up. `keep` and `orchestrator_factory` are forwarded to the
    underlying provisioning so a caller can leave the machines running or swap
    the provider for a test.
    """
    if environment.provider == "local":
        raise ValueError("a non-local environment is required")
    if scenario.backend == "containerd" and environment.provider != "multipass":
        raise ValueError(
            "containerd auto-provisioning requires a disposable Multipass VM"
        )
    if scenario.backend == "containerd" and environment.target("stack").user == "root":
        raise ValueError("containerd backend requires an unprivileged VM user")
    provider = provider_for_environment(
        environment, repo_root, orchestrator_factory=orchestrator_factory
    )
    if (
        scenario.backend == "containerd"
        and environment.containerd_maven_repository is None
    ):
        raise ValueError(
            "containerdMavenRepository must point to an isolated host Maven repository"
        )

    with TemporaryDirectory(prefix="nanolab-containerd-maven-") as staging:
        maven_source = None
        if scenario.backend == "containerd":
            if environment.containerd_maven_repository is None:
                raise ValueError("containerd Maven repository is required")
            maven_source = Path(staging) / "maven"
            stage_snapshot_repository(
                environment.containerd_maven_repository, maven_source
            )

        roles: list[ProvisionedRole] = []
        for role, request, operations in _role_requests_and_operations(
            provider,
            scenario,
            environment,
            repo_root=repo_root,
            maven_source=maven_source,
        ):
            roles.append(
                ProvisionedRole(role=role, request=request, operations=operations)
            )

        stack_request = next(entry.request for entry in roles if entry.role == "stack")
        loadgen_request = next(
            (entry.request for entry in roles if entry.role == "loadgen"), None
        )

        def after_ensure(role: str, request: VmRequest) -> None:
            if post_ensure_verifier is not None:
                post_ensure_verifier(cast(ExecutionRole, role), request)
            # `azure is not None` is guaranteed by EnvironmentConfig's validation for
            # this provider; saying it here is what lets the checker follow.
            if (
                role == "stack"
                and environment.provider == "azure"
                and environment.azure is not None
                and environment.azure.operator_source_cidr
            ):
                secure_release_endpoints(
                    environment, provider, stack_request, loadgen_request
                )

        with provision_roles(
            provider,
            tuple(roles),
            repo_root=repo_root,
            assets_root=discover_tool_root() / "assets",
            keep=keep,
            after_ensure=after_ensure,
        ):
            yield
