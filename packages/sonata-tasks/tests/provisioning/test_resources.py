from __future__ import annotations

from dataclasses import dataclass

from sonata_tasks.provisioning.resources import VerifiedLifecycle, provisioned_vm
from sonata_tasks.vm.adapters import VmLifecycleAdapter
from sonata_tasks.vm.models import VmConfig, VmInfo, VmRequest


@dataclass
class FakeProvider:
    ensured: list[VmConfig] = None  # type: ignore[assignment]
    destroyed: list[VmInfo] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.ensured: list[VmConfig] = []
        self.destroyed: list[VmInfo] = []

    def ensure_running(self, request: VmRequest) -> object:
        self.ensured.append(
            VmConfig(name=request.name or "x", cpus=request.cpus, memory=request.memory, disk=request.disk)
        )
        return _Result(return_code=0)

    def connection_host(self, request: VmRequest) -> str:
        return "10.0.0.5"

    def teardown(self, request: VmRequest) -> None:
        self.destroyed.append(VmInfo(name=request.name or "x", host="10.0.0.5", user="ubuntu", home="/home/ubuntu"))


@dataclass
class _Result:
    return_code: int


def test_verified_lifecycle_runs_verifier_after_ensure() -> None:
    calls: list[VmInfo] = []
    lifecycle = VerifiedLifecycle(
        VmLifecycleAdapter(FakeProvider(), lifecycle="multipass"),
        calls.append,
    )
    info = lifecycle.ensure_running(VmConfig(name="stack"))
    assert calls == [info]


def test_provisioned_vm_builds_resource_with_fallback_info() -> None:
    request = VmRequest(lifecycle="multipass", name="stack", host="10.0.0.5")
    resource = provisioned_vm(title="stack", request=request, provider=FakeProvider())
    assert resource.title == "stack"
