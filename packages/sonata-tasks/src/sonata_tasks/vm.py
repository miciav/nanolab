from __future__ import annotations

from sonata_engine import Resource, TaskInputs
from workflow_tasks.vm.adapters import VmLifecycleAdapter
from workflow_tasks.vm.models import VmConfig, VmInfo
from workflow_tasks.vm.tasks import DestroyVm, EnsureVmRunning


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
        _ = DestroyVm(
            task_id=f"{info.name}-destroy",
            title=f"Destroy {info.name}",
            lifecycle=lifecycle,
            info=info,
        ).run()

    def acquire(_inputs: TaskInputs) -> VmInfo:
        try:
            return ensure.run()
        except BaseException as error:
            if not external:
                try:
                    destroy(fallback_info)
                except BaseException as cleanup_error:
                    error.add_note(
                        f"Best-effort VM destroy after failed acquire failed: {cleanup_error}"
                    )
            raise

    def release(_inputs: TaskInputs, info: VmInfo) -> None:
        if not external:
            destroy(info)

    return Resource(title=title, acquire=acquire, release=release, infrastructure=True)
