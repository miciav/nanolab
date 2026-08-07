"""Environment composite: ensure role VMs, run bootstrap operations, tear down."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sonata_tasks.components.operations import RemoteCommandOperation
from sonata_tasks.provisioning.bootstrap import (
    retarget_cloud_operations,
    run_bootstrap_operations,
    scenario_context,
)
from sonata_tasks.vm.adapters import VmLifecycleAdapter
from sonata_tasks.vm.models import VmConfig, VmInfo, VmRequest, vm_remote_home
from sonata_tasks.vm.tasks import DestroyVm, EnsureVmRunning
from sonata_tasks.workflow.reporting import workflow_step


@dataclass(frozen=True)
class ProvisionedRole:
    role: str
    request: VmRequest
    operations: tuple[RemoteCommandOperation, ...] = ()


def _ensure_vm(provider: object, request: VmRequest, *, role: str) -> VmRequest:
    lifecycle = VmLifecycleAdapter(
        provider,
        lifecycle=request.lifecycle,
        credentials=request,
    )
    task = EnsureVmRunning(
        task_id=f"provision.{role}.ensure",
        title=f"Ensure {role} VM is running",
        lifecycle=lifecycle,
        config=VmConfig(
            name=request.name or request.host or role,
            cpus=request.cpus,
            memory=request.memory,
            disk=request.disk,
        ),
    )
    with workflow_step(task_id=task.task_id, title=task.title):
        task.run()
    info = task.result
    return request.model_copy(
        update={
            "lifecycle": "external",
            "host": info.host,
            "user": info.user,
            "home": info.home,
        }
    )


def _destroy_task(provider: object, request: VmRequest, *, role: str) -> DestroyVm | None:
    if request.lifecycle == "external":
        return None
    lifecycle = VmLifecycleAdapter(
        provider,
        lifecycle=request.lifecycle,
        credentials=request,
    )
    return DestroyVm(
        task_id=f"provision.{role}.destroy",
        title=f"Destroy {role} VM",
        lifecycle=lifecycle,
        info=VmInfo(
            name=request.name or role,
            host=request.host or "",
            user=request.user,
            home=vm_remote_home(request),
        ),
    )


@contextmanager
def provision_roles(
    provider: object,
    roles: tuple[ProvisionedRole, ...],
    *,
    repo_root: Path,
    assets_root: Path,
    keep: bool = False,
    after_ensure: Callable[[str, VmRequest], None] | None = None,
) -> Generator[None, None, None]:
    cleanup_tasks: list[DestroyVm] = []
    main_error: BaseException | None = None
    cleanup_error: Exception | None = None
    try:
        for entry in roles:
            cleanup = _destroy_task(provider, entry.request, role=entry.role)
            if cleanup is not None:
                cleanup_tasks.append(cleanup)
            resolved = _ensure_vm(provider, entry.request, role=entry.role)
            if after_ensure is not None:
                after_ensure(entry.role, entry.request)
            if entry.operations:
                context = scenario_context(repo_root, resolved, assets_root)
                retargeted = retarget_cloud_operations(
                    provider, context, entry.operations
                )
                run_bootstrap_operations(provider, retargeted, role=entry.role)
        yield
    except BaseException as exc:
        main_error = exc
    finally:
        try:
            if not keep:
                for task in reversed(cleanup_tasks):
                    with workflow_step(task_id=task.task_id, title=task.title):
                        task.run()
        except Exception as exc:
            cleanup_error = exc

    if main_error is not None:
        if cleanup_error is not None:
            raise RuntimeError(f"{main_error}\n\nCleanup errors:\n{cleanup_error}") from main_error
        raise main_error
    if cleanup_error is not None:
        raise cleanup_error
