"""Sonata-owned VM lifecycle for the Azure release workflow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from sonata_engine import Resource, TaskInputs
from sonata_tasks.vm import vm_resource
from workflow_tasks.components.bootstrap import (
    plan_assets_sync_to_vm,
    plan_k3s_configure_registry,
    plan_k3s_install,
    plan_loadtest_install_k6,
    plan_registry_ensure_container,
    plan_vm_provision_base,
)
from workflow_tasks.vm.adapters import VmLifecycleAdapter
from workflow_tasks.vm.models import VmConfig, VmInfo, VmRequest, vm_remote_home

from nanolab.cli.provisioning import (
    _context,
    _remote_operations,
    _retarget_cloud_operations,
    _run_operations,
)
from nanolab.cli.vm_provider import vm_request_for_role
from nanolab.config.environment import EnvironmentConfig, ExecutionRole
from nanolab.release.environment import secure_release_endpoints, verify_release_vm_facts


@dataclass(frozen=True, slots=True)
class ReleaseEndpoints:
    control_plane: str
    prometheus: str


@dataclass(frozen=True, slots=True)
class ReleaseResources:
    stack: Resource[VmInfo]
    loadgen: Resource[VmInfo]
    arm_builder: Resource[VmInfo]
    endpoints: Resource[ReleaseEndpoints]

    @property
    def vms(self) -> tuple[Resource[VmInfo], ...]:
        return self.stack, self.loadgen, self.arm_builder


class _VerifiedLifecycle:
    def __init__(
        self,
        lifecycle: VmLifecycleAdapter,
        after_ensure: Callable[[VmInfo], None],
    ) -> None:
        self._lifecycle = lifecycle
        self._after_ensure = after_ensure

    def ensure_running(self, config: VmConfig) -> VmInfo:
        info = self._lifecycle.ensure_running(config)
        self._after_ensure(info)
        return info

    def destroy(self, info: VmInfo) -> None:
        self._lifecycle.destroy(info)


def _bootstrap_role(
    environment: EnvironmentConfig,
    provider: object,
    repo_root: Path,
    role: ExecutionRole,
    request: VmRequest,
    info: VmInfo,
) -> None:
    resolved = request.model_copy(
        update={"lifecycle": "external", "host": info.host, "user": info.user, "home": info.home}
    )
    context = _context(repo_root, resolved)
    if role == "stack":
        raw = (
            *plan_vm_provision_base(context),
            *plan_k3s_install(context),
            *plan_registry_ensure_container(context),
            *plan_k3s_configure_registry(context),
        )
    elif role == "loadgen":
        raw = (*plan_loadtest_install_k6(context), *plan_assets_sync_to_vm(context))
    else:
        raw = plan_vm_provision_base(context)
    operations = _retarget_cloud_operations(environment, provider, context, _remote_operations(raw))
    _run_operations(provider, operations, role=role)


def _vm(
    *,
    title: str,
    request: VmRequest,
    provider: object,
    after_ensure: Callable[[VmInfo], None],
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[VmInfo]:
    config = VmConfig(
        name=request.name or request.host or title,
        cpus=request.cpus,
        memory=request.memory,
        disk=request.disk,
    )
    lifecycle = _VerifiedLifecycle(
        VmLifecycleAdapter(provider, lifecycle=request.lifecycle, credentials=request),
        after_ensure,
    )
    resource = vm_resource(
        title=title,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        config=config,
        fallback_info=VmInfo(
            name=config.name,
            host=request.host or "",
            user=request.user,
            home=vm_remote_home(request),
        ),
    )
    return replace(resource, requires=requires)


def build_release_resources(
    environment: EnvironmentConfig,
    repo_root: Path,
    provider: object,
) -> ReleaseResources:
    """Build three ordered VM resources without discovering cloud state."""
    stack_request = vm_request_for_role(environment, "stack", loadtest=True)
    loadgen_request = vm_request_for_role(environment, "loadgen", loadtest=True)
    arm_request = vm_request_for_role(environment, "arm-builder")

    def after_stack(info: VmInfo) -> None:
        verify_release_vm_facts(environment, provider, "stack", stack_request)
        secure_release_endpoints(environment, provider, stack_request, None)
        _bootstrap_role(environment, provider, repo_root, "stack", stack_request, info)

    def after_loadgen(info: VmInfo) -> None:
        verify_release_vm_facts(environment, provider, "loadgen", loadgen_request)
        secure_release_endpoints(environment, provider, stack_request, loadgen_request)
        _bootstrap_role(environment, provider, repo_root, "loadgen", loadgen_request, info)

    def after_arm(info: VmInfo) -> None:
        verify_release_vm_facts(environment, provider, "arm-builder", arm_request)
        provider.restrict_inbound_sources(  # type: ignore[attr-defined]
            stack_request,
            ports=(5000,),
            source_cidrs=(f"{info.host}/32",),
            priority_base=1020,
        )
        _bootstrap_role(environment, provider, repo_root, "arm-builder", arm_request, info)

    stack = _vm(
        title="Acquire release stack VM",
        request=stack_request,
        provider=provider,
        after_ensure=after_stack,
    )
    loadgen = _vm(
        title="Acquire release loadgen VM",
        request=loadgen_request,
        provider=provider,
        after_ensure=after_loadgen,
    )
    arm_builder = _vm(
        title="Acquire release ARM builder VM",
        request=arm_request,
        provider=provider,
        after_ensure=after_arm,
    )

    def acquire_endpoints(inputs: TaskInputs) -> ReleaseEndpoints:
        stack_info = inputs.resource(stack)
        _ = inputs.resource(loadgen)
        return ReleaseEndpoints(
            control_plane=f"http://{stack_info.host}:30080",
            prometheus=f"http://{stack_info.host}:30090",
        )

    endpoints = Resource(
        title="Acquire release endpoints",
        acquire=acquire_endpoints,
        release=lambda _inputs, _value: None,
        requires=(stack, loadgen),
    )
    return ReleaseResources(stack, loadgen, arm_builder, endpoints)
