"""The new runtime owns only its per-run registry, service and publications."""

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sonata_engine import TaskInputs, Workflow
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from nanolab.cli.execution import build_role_bindings
from nanolab.config.environment import EnvironmentConfig
from nanolab.tasks.containerd_rootless import (
    RootlessRun,
    control_plane_resource,
    registry_resource,
)
from nanolab.tasks.resources import ContainerdResourceCheckTask
from nanolab.tasks.vm.ports import VmCommandProvider


@dataclass
class Executor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def binding_key(self, role: str) -> str:
        return f"rootless-test:{role}"

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(
            task_id=task.task_id, status="passed", return_code=0, stdout=""
        )


def test_rootless_runtime_releases_service_before_registry() -> None:
    executor = Executor()
    run = RootlessRun(
        "run123",
        Path("/home/ubuntu/nanofaas"),
        Path("/home/ubuntu/nanolab-assets/containerd-rootless/session.sh"),
    )
    registry = registry_resource(run, executor=executor, role="stack")
    control = control_plane_resource(
        run, executor=executor, role="stack", requires=(registry,)
    )
    workflow = Workflow(workflow_id="rootless")
    workflow.add(
        CommandTask(
            title="Use platform", argv=("true",), executor=executor, role="stack"
        ),
        requires=(registry, control),
    )

    workflow.run()

    actions = [
        spec.argv[2]
        for spec in executor.seen
        if spec.argv[:2] == ("bash", str(run.script))
    ]
    assert actions == [
        "registry-start",
        "control-start",
        "control-stop",
        "registry-stop",
    ]


def test_remote_rootless_resource_uses_synced_vm_directory_not_local_cwd(tmp_path):
    class Provider:
        def __init__(self):
            self.calls = []

        def exec_argv(self, request, argv, *, env, remote_dir, dry_run):
            self.calls.append((tuple(argv), remote_dir))
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    provider = Provider()
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "owned-test-vm"}}}
    )
    bindings, _ = build_role_bindings(
        environment,
        vm_provider=Mock(spec=VmCommandProvider, wraps=provider),
        repo_root=tmp_path,
    )
    executor = RoleBoundCommandTaskExecutor(bindings)
    remote = Path("/home/ubuntu/nanofaas")
    run = RootlessRun(
        "run123",
        remote,
        Path("/home/ubuntu/nanolab-assets/containerd-rootless/session.sh"),
    )
    registry = registry_resource(run, executor=executor, role="stack")
    workflow = Workflow(workflow_id="remote-rootless")
    workflow.add(
        CommandTask(
            title="Use platform", argv=("true",), executor=executor, role="stack"
        ),
        requires=(registry,),
    )
    workflow.run()
    assert provider.calls[0] == (
        ("bash", str(run.script), "registry-start", "run123", str(remote)),
        str(remote),
    )
    assert provider.calls[-1][0][2] == "registry-stop"


def test_control_plane_receives_per_run_core_count_and_budget() -> None:
    executor = Executor()
    run = RootlessRun(
        "run123", Path("/home/ubuntu/nanofaas"), Path("/assets/session.sh")
    )
    control = control_plane_resource(
        run, executor=executor, role="stack", cpuset_cores=4, budget="12"
    )
    workflow = Workflow(workflow_id="rootless-budget")
    workflow.add(
        CommandTask(
            title="Use platform", argv=("true",), executor=executor, role="stack"
        ),
        requires=(control,),
    )

    workflow.run()

    start = next(spec for spec in executor.seen if "control-start" in spec.argv)
    assert start.argv[-2:] == ("4", "12")


def test_soak_control_plane_declares_actual_artifact_and_cgroup_limits() -> None:
    executor = Executor()
    run = RootlessRun(
        "run123", Path("/home/ubuntu/nanofaas"), Path("/assets/session.sh")
    )
    control = control_plane_resource(
        run,
        executor=executor,
        role="stack",
        mode="jvm",
        artifact=run.repo_root / "platform/control-plane/build/libs/app.jar",
        cpu=2,
        memory_bytes=1073741824,
    )
    workflow = Workflow(workflow_id="soak-artifact")
    workflow.add(
        CommandTask(
            title="Use platform", argv=("true",), executor=executor, role="stack"
        ),
        requires=(control,),
    )
    workflow.run()
    start = next(spec for spec in executor.seen if "control-start" in spec.argv)
    assert start.argv[-4:] == (
        "jvm",
        "/home/ubuntu/nanofaas/platform/control-plane/build/libs/app.jar",
        "2",
        "1073741824",
    )


def test_rootless_runtime_cleans_up_after_workload_failure() -> None:
    class FailingExecutor(Executor):
        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            self.seen.append(task)
            return TaskResult(
                task_id=task.task_id,
                status="failed" if task.argv == ("false",) else "passed",
                return_code=1 if task.argv == ("false",) else 0,
                stdout="",
            )

    executor = FailingExecutor()
    run = RootlessRun(
        "run123", Path("/home/ubuntu/nanofaas"), Path("/assets/session.sh")
    )
    registry = registry_resource(run, executor=executor, role="stack")
    control = control_plane_resource(
        run, executor=executor, role="stack", requires=(registry,)
    )
    workflow = Workflow(workflow_id="rootless-failure")
    workflow.add(
        CommandTask(title="Fail", argv=("false",), executor=executor, role="stack"),
        requires=(registry, control),
    )

    with contextlib.suppress(RuntimeError):
        workflow.run()

    actions = [
        spec.argv[2]
        for spec in executor.seen
        if spec.argv[:2] == ("bash", str(run.script))
    ]
    assert actions[-2:] == ["control-stop", "registry-stop"]


def test_registry_prepare_failure_still_releases_owned_port_and_container() -> None:
    class PrepareFails(Executor):
        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            self.seen.append(task)
            failed = "registry-start" in task.argv
            return TaskResult(
                task_id=task.task_id,
                status="failed" if failed else "passed",
                return_code=1 if failed else 0,
                stdout="",
            )

    executor = PrepareFails()
    run = RootlessRun(
        "run123", Path("/home/ubuntu/nanofaas"), Path("/assets/session.sh")
    )
    workflow = Workflow(workflow_id="prepare-failure")
    registry = registry_resource(run, executor=executor, role="stack")
    workflow.add(
        CommandTask(
            title="Use registry", argv=("true",), executor=executor, role="stack"
        ),
        requires=(registry,),
    )

    with pytest.raises(RuntimeError):
        workflow.run()

    actions = [
        spec.argv[2]
        for spec in executor.seen
        if spec.argv[:2] == ("bash", str(run.script))
    ]
    assert actions == ["registry-start", "registry-stop"]
    assert all(spec.argv != ("true",) for spec in executor.seen)


def test_containerd_resource_check_reads_actual_oci_limits() -> None:
    class InspectExecutor(Executor):
        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            self.seen.append(task)
            return TaskResult(
                task_id=task.task_id,
                status="passed",
                return_code=0,
                stdout='{"ID":"nanofaas-word-stats-java-0123456789-r1","Spec":{"linux":{"resources":{"cpu":{"shares":512,"quota":50000,"period":100000},"memory":{"limit":536870912,"reservation":268435456}}}}}',
            )

    executor = InspectExecutor()
    task = ContainerdResourceCheckTask(
        function="word-stats-java",
        replica=1,
        resources={
            "requests": {"cpu": 0.5, "memoryMiB": 256},
            "limits": {"cpu": 0.5, "memoryMiB": 512},
        },
        run=RootlessRun(
            "run123", Path("/home/ubuntu/nanofaas"), Path("/assets/session.sh")
        ),
        executor=executor,
        role="stack",
    )

    task.run(TaskInputs.empty())
    assert executor.seen[0].argv[2] == "inspect-owned"
    assert executor.seen[0].argv[-2:] == ("word-stats-java", "1")


def test_containerd_resource_check_rejects_missing_memory_limit() -> None:
    class InspectExecutor(Executor):
        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            return TaskResult(
                task_id=task.task_id,
                status="passed",
                return_code=0,
                stdout='{"ID":"nanofaas-echo-0123456789-r1","Spec":{"linux":{"resources":{"memory":{}}}}}',
            )

    task = ContainerdResourceCheckTask(
        function="echo",
        replica=1,
        resources={"limits": {"memoryMiB": 512}},
        run=RootlessRun(
            "run123", Path("/home/ubuntu/nanofaas"), Path("/assets/session.sh")
        ),
        executor=InspectExecutor(),
        role="stack",
    )

    with pytest.raises(
        RuntimeError, match=r"memory\.limit is None, expected 536870912"
    ):
        task.run(TaskInputs.empty())
