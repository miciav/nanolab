"""Environment composite: ensure role VMs, run bootstrap operations, tear down."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from nanolab.tasks.components.operations import RemoteCommandOperation
from nanolab.tasks.provisioning.bootstrap import (
    retarget_cloud_operations,
    run_bootstrap_operations,
    scenario_context,
)
from sonata_tasks.vm.adapters import VmLifecycleAdapter
from nanolab.tasks.vm.models import VmConfig, VmInfo, VmRequest, vm_remote_home
from sonata_tasks.vm.tasks import DestroyVm, EnsureVmRunning
from sonata_engine.workflow.reporting import subtask


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
    with subtask(task_id=task.task_id, title=task.title):
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


def _run_cleanup_tasks(cleanup_tasks: list[DestroyVm]) -> list[str]:
    """Tear down in reverse order, collecting RuntimeError messages."""
    cleanup_errors: list[str] = []
    for task in reversed(cleanup_tasks):
        try:
            with subtask(task_id=task.task_id, title=task.title):
                task.run()
        except RuntimeError as exc:
            cleanup_errors.append(str(exc))
    return cleanup_errors


def _raise_if_failed(
    main_error: BaseException | None,
    cleanup_errors: list[str],
) -> None:
    """Translate a main error and any cleanup failures into the final exception."""
    if main_error is not None:
        if cleanup_errors:
            combined = f"{main_error}\n\nCleanup errors:\n" + "\n".join(cleanup_errors)
            raise RuntimeError(combined) from main_error
        raise main_error
    if cleanup_errors:
        raise RuntimeError("Cleanup failed:\n" + "\n".join(cleanup_errors))


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
    cleanup_errors: list[str] = []
    try:
        resolved: list[VmRequest] = []
        for entry in roles:
            cleanup = _destroy_task(provider, entry.request, role=entry.role)
            if cleanup is not None:
                cleanup_tasks.append(cleanup)
            resolved.append(_ensure_vm(provider, entry.request, role=entry.role))
        for entry, request in zip(roles, resolved):
            if after_ensure is not None:
                after_ensure(entry.role, entry.request)
        for entry, request in zip(roles, resolved):
            if entry.operations:
                context = scenario_context(repo_root, request, assets_root)
                retargeted = retarget_cloud_operations(
                    provider, context, entry.operations
                )
                run_bootstrap_operations(provider, retargeted, role=entry.role)
        yield
    except BaseException as exc:  # NOSONAR (S5754): cleanup must run across the yield boundary
        main_error = exc
    finally:
        if not keep:
            cleanup_errors = _run_cleanup_tasks(cleanup_tasks)

    _raise_if_failed(main_error, cleanup_errors)
