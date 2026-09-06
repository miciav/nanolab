"""VM resources for provisioning: acquire, verify, release per role."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from sonata_engine import Resource

from sonata_tasks.vm import vm_resource
from sonata_tasks.vm.adapters import VmLifecycleAdapter
from nanolab.tasks.vm.models import VmConfig, VmInfo, VmRequest, vm_remote_home


class VerifiedLifecycle:
    """VmLifecycleProtocol wrapper that runs a verifier after each acquire."""

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


def provisioned_vm(
    *,
    title: str,
    request: VmRequest,
    provider: object,
    after_ensure: Callable[[VmInfo], None] | None = None,
    requires: tuple[Resource[Any], ...] = (),
    external: bool = False,
) -> Resource[VmInfo]:
    """A Sonata VM resource that ensures, verifies, and compensates the VM."""
    config = VmConfig(
        name=request.name or request.host or title,
        cpus=request.cpus,
        memory=request.memory,
        disk=request.disk,
    )
    lifecycle = VerifiedLifecycle(
        VmLifecycleAdapter(provider, lifecycle=request.lifecycle, credentials=request),
        after_ensure or (lambda _info: None),
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
        external=external,
    )
    return replace(resource, requires=requires)
