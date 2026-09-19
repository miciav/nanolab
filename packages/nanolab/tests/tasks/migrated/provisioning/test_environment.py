from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_tasks.tasks.models import TaskResult
from sonata_tasks.vm.models import VmRequest as SharedVmRequest

from nanolab.tasks.components.operations import RemoteCommandOperation
from nanolab.tasks.provisioning.environment import ProvisionedRole, provision_roles
from nanolab.tasks.vm.models import VmRequest


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
    ensured: list[SharedVmRequest] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)
    destroy_failures: set[str] = field(default_factory=set)
    events: list[str] = field(default_factory=list)
    present: set[str] = field(default_factory=set)
    fail_after_create: str | None = None

    def vm_exists(self, request: SharedVmRequest) -> bool:
        self.events.append(f"exists:{request.name or '?'}")
        return (request.name or "?") in self.present

    def ensure_running(self, request: SharedVmRequest) -> _Result:
        self.ensured.append(request)
        self.events.append(f"ensure:{request.name or '?'}")
        self.present.add(request.name or "?")
        if request.name == self.fail_after_create:
            raise RuntimeError("ensure failed after launch")
        return _Result(return_code=0)

    def connection_host(self, request: SharedVmRequest) -> str:
        return "10.0.0.5"

    def teardown(self, request: SharedVmRequest) -> _Result:
        if request.name in self.destroy_failures:
            raise RuntimeError(f"destroy {request.name} failed")
        self.destroyed.append(request.name or "?")
        self.present.discard(request.name or "?")
        self.events.append(f"destroy:{request.name or '?'}")
        return _Result(return_code=0)


@dataclass
class _Result:
    return_code: int
    stdout: str = ""
    stderr: str = ""


def test_provision_roles_ensures_runs_operations_and_destroys(tmp_path) -> None:
    provider = FakeOrchestrator()
    request = VmRequest(lifecycle="multipass", name="stack")
    op = RemoteCommandOperation(
        operation_id="k3s", summary="install", argv=("helm", "install")
    )
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


def test_provision_roles_destroy_failure_keeps_destroying_and_aggregates(
    tmp_path,
) -> None:
    provider = FakeOrchestrator()
    provider.destroy_failures = {"loadgen"}
    with (
        pytest.raises(RuntimeError) as excinfo,
        provision_roles(
            provider,
            (
                ProvisionedRole(
                    role="stack", request=VmRequest(lifecycle="multipass", name="stack")
                ),
                ProvisionedRole(
                    role="loadgen",
                    request=VmRequest(lifecycle="multipass", name="loadgen"),
                ),
            ),
            repo_root=tmp_path,
            assets_root=tmp_path / "assets",
        ),
    ):
        raise RuntimeError("main exploded")
    assert provider.destroyed == ["stack"]
    assert "main exploded" in str(excinfo.value)
    assert "destroy loadgen failed" in str(excinfo.value)


def test_provision_roles_cleanup_error_without_main_error(tmp_path) -> None:
    provider = FakeOrchestrator()
    provider.destroy_failures = {"stack"}
    with (
        pytest.raises(RuntimeError) as excinfo,
        provision_roles(
            provider,
            (
                ProvisionedRole(
                    role="stack", request=VmRequest(lifecycle="multipass", name="stack")
                ),
            ),
            repo_root=tmp_path,
            assets_root=tmp_path / "assets",
        ),
    ):
        pass
    assert "Cleanup failed:" in str(excinfo.value)
    assert "destroy stack failed" in str(excinfo.value)


def test_provision_roles_propagates_programming_errors_from_destroy(tmp_path) -> None:
    class BrokenOrchestrator(FakeOrchestrator):
        def teardown(self, request: SharedVmRequest) -> _Result:
            raise ValueError("bad teardown contract")

    with (
        pytest.raises(ValueError, match="bad teardown contract"),
        provision_roles(
            BrokenOrchestrator(),
            (
                ProvisionedRole(
                    role="stack", request=VmRequest(lifecycle="multipass", name="stack")
                ),
            ),
            repo_root=tmp_path,
            assets_root=tmp_path / "assets",
        ),
    ):
        pass


def test_provision_roles_ensures_all_before_verify_then_operations(tmp_path) -> None:
    events: list[str] = []
    provider = FakeOrchestrator(events=events, shell=RecordingShell(events=events))
    op = RemoteCommandOperation(
        operation_id="k3s", summary="install", argv=("helm", "install")
    )
    with provision_roles(
        provider,
        (
            ProvisionedRole(
                role="stack",
                request=VmRequest(lifecycle="multipass", name="stack"),
                operations=(op,),
            ),
            ProvisionedRole(
                role="loadgen",
                request=VmRequest(lifecycle="multipass", name="loadgen"),
                operations=(op,),
            ),
        ),
        repo_root=tmp_path,
        assets_root=tmp_path / "assets",
        after_ensure=lambda role, _request: events.append(f"verify:{role}"),
    ):
        pass
    assert events == [
        "exists:stack",
        "ensure:stack",
        "exists:loadgen",
        "ensure:loadgen",
        "verify:stack",
        "verify:loadgen",
        "command:helm",
        "command:helm",
        "destroy:loadgen",
        "destroy:stack",
    ]


def test_existing_vm_survives_bootstrap_failure(tmp_path) -> None:
    provider = FakeOrchestrator(present={"stack"})
    with (
        pytest.raises(RuntimeError, match="bootstrap failed"),
        provision_roles(
            provider,
            (ProvisionedRole("stack", VmRequest(lifecycle="multipass", name="stack")),),
            repo_root=tmp_path,
            assets_root=tmp_path / "assets",
        ),
    ):
        raise RuntimeError("bootstrap failed")
    assert provider.present == {"stack"}
    assert provider.destroyed == []


def test_new_vm_is_cleaned_when_ensure_fails_after_launch(tmp_path) -> None:
    provider = FakeOrchestrator(fail_after_create="stack")
    with (
        pytest.raises(RuntimeError, match="ensure failed after launch"),
        provision_roles(
            provider,
            (ProvisionedRole("stack", VmRequest(lifecycle="multipass", name="stack")),),
            repo_root=tmp_path,
            assets_root=tmp_path / "assets",
        ),
    ):
        pass
    assert provider.present == set()
    assert provider.destroyed == ["stack"]


def test_unknown_vm_ownership_fails_before_ensure_or_teardown(tmp_path) -> None:
    class UncertainOrchestrator(FakeOrchestrator):
        def vm_exists(self, request: SharedVmRequest) -> bool:
            raise RuntimeError("provider lookup failed")

    provider = UncertainOrchestrator()
    with (
        pytest.raises(RuntimeError, match="provider lookup failed"),
        provision_roles(
            provider,
            (ProvisionedRole("stack", VmRequest(lifecycle="multipass", name="stack")),),
            repo_root=tmp_path,
            assets_root=tmp_path / "assets",
        ),
    ):
        pass
    assert provider.ensured == []
    assert provider.destroyed == []
