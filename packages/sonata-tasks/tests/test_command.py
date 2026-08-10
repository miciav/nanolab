from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sonata_engine import Resource, TaskInputs, TaskOutcome
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult
from sonata_tasks.vm.models import VmInfo

from sonata_tasks.command import CommandTask


@dataclass
class RecordingExecutor:
    """Records the specs it is handed and replays a canned result."""

    result: TaskResult = field(
        default_factory=lambda: TaskResult(task_id="", status="passed", return_code=0)
    )
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return self.result


def test_a_passing_command_carries_its_result_as_the_outcome_value() -> None:
    executor = RecordingExecutor(
        result=TaskResult(task_id="", status="passed", return_code=0, stdout="ok")
    )
    task = CommandTask(title="List functions", argv=("cli", "fn", "list"), executor=executor)

    outcome = task.run(TaskInputs.empty())

    assert isinstance(outcome, TaskOutcome)
    assert outcome.value is not None
    assert outcome.value.stdout == "ok"


def test_the_spec_carries_no_task_id_because_identity_belongs_to_the_compiler() -> None:
    executor = RecordingExecutor()
    CommandTask(title="List functions", argv=("cli",), executor=executor).run(TaskInputs.empty())

    assert executor.seen[0].task_id == ""
    assert executor.seen[0].summary == "List functions"


def test_role_and_cwd_reach_the_spec() -> None:
    executor = RecordingExecutor()
    task = CommandTask(
        title="Build",
        argv=("./gradlew", "installDist"),
        executor=executor,
        role="stack",
        cwd=Path("/repo"),
    )

    task.run(TaskInputs.empty())

    assert executor.seen[0].execution_role == "stack"
    assert executor.seen[0].cwd == Path("/repo")


def test_remote_dir_and_timeout_reach_the_spec() -> None:
    executor = RecordingExecutor()
    task = CommandTask(
        title="Remote build",
        argv=("./gradlew", "test"),
        executor=executor,
        role="stack",
        remote_dir="/srv/nanofaas/source",
        timeout_seconds=900,
    )

    task.run(TaskInputs.empty())

    assert executor.seen[0].remote_dir == "/srv/nanofaas/source"
    assert executor.seen[0].timeout_seconds == 900


def test_a_failing_command_raises_with_stderr_in_the_message() -> None:
    executor = RecordingExecutor(
        result=TaskResult(task_id="", status="failed", return_code=2, stderr="no such function")
    )
    task = CommandTask(title="Invoke thing", argv=("cli",), executor=executor)

    with pytest.raises(RuntimeError, match="Invoke thing failed \\(exit 2\\): no such function"):
        task.run(TaskInputs.empty())


def test_a_failing_command_falls_back_to_stdout_then_to_a_placeholder() -> None:
    stdout_only = RecordingExecutor(
        result=TaskResult(task_id="", status="failed", return_code=1, stdout="bad request")
    )
    with pytest.raises(RuntimeError, match="bad request"):
        CommandTask(title="Apply", argv=("cli",), executor=stdout_only).run(TaskInputs.empty())

    silent = RecordingExecutor(result=TaskResult(task_id="", status="failed", return_code=1))
    with pytest.raises(RuntimeError, match="no output"):
        CommandTask(title="Apply", argv=("cli",), executor=silent).run(TaskInputs.empty())


def test_a_failing_command_keeps_stdout_when_stderr_is_present() -> None:
    executor = RecordingExecutor(
        result=TaskResult(task_id="", status="failed", return_code=22, stderr="curl: (22)", stdout="Kubernetes client unavailable")
    )

    with pytest.raises(RuntimeError, match=r"(?s)curl: \(22\).*Kubernetes client unavailable"):
        CommandTask(title="Register", argv=("curl",), executor=executor).run(TaskInputs.empty())


def test_expected_exit_codes_reach_the_spec() -> None:
    executor = RecordingExecutor()
    task = CommandTask(
        title="Delete",
        argv=("cli",),
        executor=executor,
        expected_exit_codes=frozenset({0, 1}),
    )

    task.run(TaskInputs.empty())

    assert executor.seen[0].expected_exit_codes == frozenset({0, 1})


def test_the_verify_hook_runs_on_success_and_can_reject_the_result() -> None:
    executor = RecordingExecutor(
        result=TaskResult(task_id="", status="passed", return_code=0, stdout="{}")
    )

    def reject(result: TaskResult) -> None:
        raise RuntimeError(f"unusable payload: {result.stdout}")

    task = CommandTask(title="Invoke", argv=("cli",), executor=executor, verify=reject)

    with pytest.raises(RuntimeError, match="unusable payload"):
        task.run(TaskInputs.empty())


def test_the_verify_hook_is_skipped_when_the_command_itself_failed() -> None:
    executor = RecordingExecutor(
        result=TaskResult(task_id="", status="failed", return_code=1, stderr="boom")
    )
    calls: list[TaskResult] = []

    task = CommandTask(title="Invoke", argv=("cli",), executor=executor, verify=calls.append)

    with pytest.raises(RuntimeError, match="boom"):
        task.run(TaskInputs.empty())
    assert calls == []


def test_a_runtime_argv_resolver_can_read_a_declared_vm_resource() -> None:
    vm = Resource[VmInfo](
        "Acquire VM", acquire=lambda _inputs: pytest.fail(), release=lambda *_: None
    )
    info = VmInfo(name="worker", host="10.0.0.7", user="ubuntu", home="/home/ubuntu")
    inputs = TaskInputs._for_resources({vm: info}, {vm})
    executor = RecordingExecutor()

    task = CommandTask(
        title="Connect",
        argv=lambda task_inputs: ("ssh", task_inputs.resource(vm).host),
        executor=executor,
    )

    task.run(inputs)

    assert executor.seen[0].argv == ("ssh", "10.0.0.7")


def test_runtime_argv_resolver_runs_once_and_env_reaches_the_spec() -> None:
    calls: list[TaskInputs] = []
    executor = RecordingExecutor()
    inputs = TaskInputs.empty()

    def argv(task_inputs: TaskInputs) -> tuple[str, ...]:
        calls.append(task_inputs)
        return ("cli", "fn", "list")

    CommandTask(
        title="List functions",
        argv=argv,
        env={"REGION": "eu-west-1"},
        executor=executor,
    ).run(inputs)

    assert calls == [inputs]
    assert executor.seen[0].env == {"REGION": "eu-west-1"}


def test_a_runtime_argv_resolver_error_prevents_executor_invocation() -> None:
    executor = RecordingExecutor()

    def argv(_inputs: TaskInputs) -> tuple[str, ...]:
        raise ValueError("VM address unavailable")

    with pytest.raises(ValueError, match="VM address unavailable"):
        CommandTask(title="Connect", argv=argv, executor=executor).run(TaskInputs.empty())

    assert executor.seen == []
