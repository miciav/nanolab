from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sonata_tasks.components.context import ScenarioExecutionContext
from sonata_tasks.components.operations import RemoteCommandOperation
from sonata_tasks.provisioning.bootstrap import (
    remote_operations,
    retarget_cloud_operations,
    run_bootstrap_operations,
    scenario_context,
)
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult
from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.proxmox import ProxmoxVmProvider


@dataclass
class RecordingShell:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, argv: list[str], *, cwd, env, dry_run: bool) -> TaskResult:
        self.seen.append(CommandTaskSpec(task_id="x", summary="x", argv=tuple(argv)))
        return TaskResult(task_id="x", status="passed", return_code=0)


@dataclass
class FakeOrchestrator(ProxmoxVmProvider):
    shell: RecordingShell = field(default_factory=RecordingShell)
    retargeted: list[str] = field(default_factory=list)

    def ssh_endpoint(self, request: VmRequest) -> tuple[str, int]:
        return "10.0.0.5", 22

    def ssh_private_key_path(self, request: VmRequest) -> str:
        return "/keys/id_rsa"


def test_scenario_context_carries_request() -> None:
    request = VmRequest(lifecycle="multipass", name="stack")
    context = scenario_context(Path("/repo"), request, Path("/assets"))
    assert context.vm_request == request
    assert context.assets_root == Path("/assets")


def test_remote_operations_filters_non_remote() -> None:
    op = RemoteCommandOperation(operation_id="a", summary="s", argv=("true",))
    assert remote_operations([op]) == (op,)


def test_run_bootstrap_operations_records_each_command() -> None:
    provider = FakeOrchestrator()
    op = RemoteCommandOperation(operation_id="k3s", summary="install", argv=("helm", "install"))
    run_bootstrap_operations(provider, [op], role="stack")
    assert provider.shell.seen[0].argv == ("helm", "install")


def test_retarget_cloud_operations_uses_ssh_endpoint() -> None:
    provider = FakeOrchestrator()
    context = scenario_context(Path("/repo"), VmRequest(lifecycle="proxmox", name="stack"), Path("/assets"))
    op = RemoteCommandOperation(
        operation_id="base",
        summary="base",
        argv=("ansible-playbook", "-i", "unused", "playbook.yml"),
    )
    retargeted = retarget_cloud_operations(provider, context, [op])
    assert "-e" in retargeted[0].argv
    assert "ansible_port=22" in retargeted[0].argv


def test_retarget_cloud_operations_branches_on_provider_not_lifecycle() -> None:
    # The release flow resolves the request to lifecycle="external" before
    # retargeting, so the branch must follow the provider type, not the request.
    provider = FakeOrchestrator()
    context = scenario_context(
        Path("/repo"),
        VmRequest(lifecycle="external", name="stack", host="10.0.0.5"),
        Path("/assets"),
    )
    op = RemoteCommandOperation(
        operation_id="base",
        summary="base",
        argv=("ansible-playbook", "-i", "unused", "playbook.yml"),
    )
    retargeted = retarget_cloud_operations(provider, context, [op])
    assert "ansible_port=22" in retargeted[0].argv
