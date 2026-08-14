from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from sonata_tasks.components.operations import RemoteCommandOperation
from sonata_tasks.provisioning.environment import ProvisionedRole, provision_roles
from sonata_tasks.tasks.models import TaskResult
from sonata_tasks.vm.models import VmRequest


@dataclass
class RecordingShell:
    commands: list[tuple[str, ...]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def run(self, argv: list[str], *, cwd, env, dry_run: bool) -> TaskResult:
        self.commands.append(tuple(argv))
        self.events.append(f"command:{argv[0]}")
        return TaskResult(task_id="x", status="passed", return_code=0)


@dataclass
class FakeOrchestrator:
    shell: RecordingShell = field(default_factory=RecordingShell)
    ensured: list[VmRequest] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)
    destroy_failures: set[str] = field(default_factory=set)
    events: list[str] = field(default_factory=list)

    def ensure_running(self, request: VmRequest) -> object:
        self.ensured.append(request)
        self.events.append(f"ensure:{request.name or '?'}")
        return _Result(return_code=0)

    def connection_host(self, request: VmRequest) -> str:
        return "10.0.0.5"

    def teardown(self, request: VmRequest) -> None:
        if request.name in self.destroy_failures:
            raise RuntimeError(f"destroy {request.name} failed")
        self.destroyed.append(request.name or "?")
        self.events.append(f"destroy:{request.name or '?'}")


@dataclass
class _Result:
    return_code: int


def test_provision_roles_ensures_runs_operations_and_destroys(tmp_path) -> None:
    provider = FakeOrchestrator()
    request = VmRequest(lifecycle="multipass", name="stack")
    op = RemoteCommandOperation(operation_id="k3s", summary="install", argv=("helm", "install"))
    with provision_roles(
        provider,
        (ProvisionedRole(role="stack", request=request, operations=(op,)),),
        repo_root=tmp_path,
        assets_root=tmp_path / "assets",
    ):
        pass
    assert [r.name for r in provider.ensured] == ["stack"]
    assert provider.shell.commands == [("helm", "install")]
    assert provider.destroyed == ["stack"]


def test_provision_roles_keep_skips_teardown(tmp_path) -> None:
    provider = FakeOrchestrator()
    request = VmRequest(lifecycle="multipass", name="stack")
    with provision_roles(
        provider,
        (ProvisionedRole(role="stack", request=request),),
        repo_root=tmp_path,
        assets_root=tmp_path / "assets",
        keep=True,
    ):
        pass
    assert provider.destroyed == []


def test_provision_roles_destroy_failure_keeps_destroying_and_aggregates(tmp_path) -> None:
    provider = FakeOrchestrator()
    provider.destroy_failures = {"loadgen"}
    with pytest.raises(RuntimeError) as excinfo:
        with provision_roles(
            provider,
            (
                ProvisionedRole(role="stack", request=VmRequest(lifecycle="multipass", name="stack")),
                ProvisionedRole(role="loadgen", request=VmRequest(lifecycle="multipass", name="loadgen")),
            ),
            repo_root=tmp_path,
            assets_root=tmp_path / "assets",
        ):
            raise RuntimeError("main exploded")
    assert provider.destroyed == ["stack"]
    assert "main exploded" in str(excinfo.value)
    assert "destroy loadgen failed" in str(excinfo.value)


def test_provision_roles_cleanup_error_without_main_error(tmp_path) -> None:
    provider = FakeOrchestrator()
    provider.destroy_failures = {"stack"}
    with pytest.raises(RuntimeError) as excinfo:
        with provision_roles(
            provider,
            (ProvisionedRole(role="stack", request=VmRequest(lifecycle="multipass", name="stack")),),
            repo_root=tmp_path,
            assets_root=tmp_path / "assets",
        ):
            pass
    assert "Cleanup failed:" in str(excinfo.value)
    assert "destroy stack failed" in str(excinfo.value)


def test_provision_roles_propagates_programming_errors_from_destroy(tmp_path) -> None:
    class BrokenOrchestrator(FakeOrchestrator):
        def teardown(self, request: VmRequest) -> None:
            raise ValueError("bad teardown contract")

    with pytest.raises(ValueError, match="bad teardown contract"):
        with provision_roles(
            BrokenOrchestrator(),
            (ProvisionedRole(role="stack", request=VmRequest(lifecycle="multipass", name="stack")),),
            repo_root=tmp_path,
            assets_root=tmp_path / "assets",
        ):
            pass


def test_provision_roles_ensures_all_before_verify_then_operations(tmp_path) -> None:
    events: list[str] = []
    provider = FakeOrchestrator(events=events, shell=RecordingShell(events=events))
    op = RemoteCommandOperation(operation_id="k3s", summary="install", argv=("helm", "install"))
    with provision_roles(
        provider,
        (
            ProvisionedRole(role="stack", request=VmRequest(lifecycle="multipass", name="stack"), operations=(op,)),
            ProvisionedRole(role="loadgen", request=VmRequest(lifecycle="multipass", name="loadgen"), operations=(op,)),
        ),
        repo_root=tmp_path,
        assets_root=tmp_path / "assets",
        after_ensure=lambda role, _request: events.append(f"verify:{role}"),
    ):
        pass
    assert events == [
        "ensure:stack",
        "ensure:loadgen",
        "verify:stack",
        "verify:loadgen",
        "command:helm",
        "command:helm",
        "destroy:loadgen",
        "destroy:stack",
    ]
