from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from typing import cast

import pytest

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


def test_run_bootstrap_operations_keeps_stdout_failure_alongside_stderr() -> None:
    @dataclass
    class FailingShell:
        def run(self, command: list[str], /, *, cwd, env, dry_run: bool) -> TaskResult:
            return TaskResult(
                task_id="x", status="failed", return_code=2,
                stdout="fatal: k6 download failed", stderr="warning: remote_tmp"
            )

    provider = FakeOrchestrator(shell=cast(RecordingShell, FailingShell()))
    with pytest.raises(RuntimeError, match="fatal: k6 download failed") as error:
        run_bootstrap_operations(
            provider, [RemoteCommandOperation(operation_id="k6", summary="install", argv=("k6",))], role="loadgen"
        )
    assert "warning: remote_tmp" in str(error.value)


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


def test_retarget_cloud_operations_repoints_synthetic_host_for_non_cloud() -> None:
    # Multipass plans target the synthetic "{name}.internal" host; the
    # post-ensure retarget must land the real connection host in both the
    # ansible inventory and the rsync target.
    context = scenario_context(
        Path("/repo"),
        VmRequest(lifecycle="external", name="stack", host="10.0.0.5"),
        Path("/assets"),
    )
    ops = (
        RemoteCommandOperation(
            operation_id="base",
            summary="base",
            argv=("ansible-playbook", "-i", "stack.internal,", "playbook.yml"),
        ),
        RemoteCommandOperation(
            operation_id="repo.sync_to_vm",
            summary="sync",
            argv=("rsync", "-az", "repo/", "ubuntu@stack.internal:/home/ubuntu/nanofaas/"),
        ),
    )
    retargeted = retarget_cloud_operations(object(), context, ops)
    assert retargeted[0].argv[retargeted[0].argv.index("-i") + 1] == "10.0.0.5,"
    assert retargeted[1].argv[-1] == "ubuntu@10.0.0.5:/home/ubuntu/nanofaas/"


def test_retarget_cloud_operations_skips_placeholder_resolution() -> None:
    # When the resolved host is still the synthetic placeholder there is no
    # real host to substitute, and pre-ensure retargets must be preserved.
    context = scenario_context(
        Path("/repo"),
        VmRequest(lifecycle="external", name="stack", host="stack.internal"),
        Path("/assets"),
    )
    op = RemoteCommandOperation(
        operation_id="base",
        summary="base",
        argv=("ansible-playbook", "-i", "pve.example,", "playbook.yml"),
    )
    assert retarget_cloud_operations(object(), context, [op]) == (op,)
