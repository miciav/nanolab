from __future__ import annotations

from sonata_engine import Resource, TaskInputs
from sonata_tasks.vm.adapters import VmLifecycleAdapter
from sonata_tasks.vm.models import VmConfig, VmInfo
from sonata_tasks.vm.tasks import DestroyVm, EnsureVmRunning

from sonata_tasks.compensation import compensated_resource


def vm_resource(
    *,
    title: str,
    lifecycle: VmLifecycleAdapter,
    config: VmConfig,
    fallback_info: VmInfo,
    external: bool = False,
) -> Resource[VmInfo]:
    """Expose VM lifecycle operations as a Sonata infrastructure resource."""

    ensure = EnsureVmRunning(
        task_id=f"{config.name}-ensure",
        title=title,
        lifecycle=lifecycle,
        config=config,
    )

    def destroy(info: VmInfo) -> None:
        DestroyVm(
            task_id=f"{info.name}-destroy",
            title=f"Destroy {info.name}",
            lifecycle=lifecycle,
            info=info,
        ).run()

    def acquire(_inputs: TaskInputs) -> VmInfo:
        return ensure.run()

    def compensate(_inputs: TaskInputs) -> None:
        # fallback_info, not the acquired one: the acquire failed, so there is no
        # VmInfo to destroy — only the VM it was trying to create.
        if not external:
            destroy(fallback_info)

    def release(_inputs: TaskInputs, info: VmInfo) -> None:
        if not external:
            destroy(info)

    return compensated_resource(
        title=title,
        acquire=acquire,
        compensate=compensate,
        release=release,
        # The journal can only hold JSON, so a run that kept this VM recorded its
        # info as a dict. A later teardown needs the dataclass back, because that
        # is what destroy() reads a name off.
        revive=lambda raw: VmInfo(**raw),
    )
