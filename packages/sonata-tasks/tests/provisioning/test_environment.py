from __future__ import annotations

from dataclasses import dataclass, field

from sonata_tasks.components.operations import RemoteCommandOperation
from sonata_tasks.provisioning.environment import ProvisionedRole, provision_roles
from sonata_tasks.tasks.models import TaskResult
from sonata_tasks.vm.models import VmRequest


@dataclass
class RecordingShell:
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, argv: list[str], *, cwd, env, dry_run: bool) -> TaskResult:
        self.commands.append(tuple(argv))
        return TaskResult(task_id="x", status="passed", return_code=0)


@dataclass
class FakeOrchestrator:
    shell: RecordingShell = field(default_factory=RecordingShell)
    ensured: list[VmRequest] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)

    def ensure_running(self, request: VmRequest) -> object:
        self.ensured.append(request)
        return _Result(return_code=0)

    def connection_host(self, request: VmRequest) -> str:
        return "10.0.0.5"

    def teardown(self, request: VmRequest) -> None:
        self.destroyed.append(request.name or "?")


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
