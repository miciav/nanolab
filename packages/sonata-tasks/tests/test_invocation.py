from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.cli_function import CliFunctionInvokeTask
from sonata_tasks.http_function import HttpFunctionInvokeTask
from sonata_tasks.invocation import verify_invocation

SUCCESS = '{"status":"success","output":"5 words"}'


@dataclass
class RecordingExecutor:
    stdout: str = SUCCESS
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=self.stdout)


def _result(stdout: str) -> TaskResult:
    return TaskResult(task_id="", status="passed", return_code=0, stdout=stdout)


def test_a_successful_response_passes() -> None:
    verify_invocation(_result(SUCCESS))


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("<html>502</html>", "was not JSON"),
        ('["success"]', "was not JSON object"),
        ('{"status":"error","output":""}', "did not report success"),
        ('{"status":"success"}', "carried no output"),
    ],
)
def test_it_separates_the_ways_an_invocation_can_be_unusable(stdout: str, expected: str) -> None:
    """The shell version piped this through two `grep -q` calls, which reported
    every one of these four cases identically."""
    with pytest.raises(RuntimeError, match=expected):
        verify_invocation(_result(stdout))


def test_http_invoke_posts_the_payload_to_the_invoke_endpoint() -> None:
    executor = RecordingExecutor()

    task = HttpFunctionInvokeTask(
        "word-stats",
        payload='{"text":"a b"}',
        endpoint="http://127.0.0.1:18080",
        executor=executor,
        role="host",
    )
    _ = task.run(TaskInputs.empty())

    assert task.title == "Invoke word-stats"
    assert executor.seen[0].argv == (
        "curl",
        "-fsS",
        "-H",
        "Content-Type: application/json",
        "--data",
        '{"text":"a b"}',
        "http://127.0.0.1:18080/v1/functions/word-stats:invoke",
    )


def test_http_invoke_survives_a_shell_when_it_runs_inside_a_vm() -> None:
    executor = RecordingExecutor()

    _ = HttpFunctionInvokeTask(
        "word-stats",
        payload='{"text":"a b"}',
        endpoint="http://cp:8080",
        executor=executor,
        role="stack",
        through_shell=True,
    ).run(TaskInputs.empty())

    argv = executor.seen[0].argv
    assert argv[:2] == ("bash", "-lc")
    assert '"--data" "{\\"text\\":\\"a b\\"}"' in argv[2]


def test_cli_invoke_drives_the_binary_instead_of_curl() -> None:
    executor = RecordingExecutor()

    _ = CliFunctionInvokeTask(
        "word-stats",
        payload='{"text":"a b"}',
        cli_argv=("nanofaas", "--endpoint", "http://cp:8080"),
        executor=executor,
        role="stack",
    ).run(TaskInputs.empty())

    assert executor.seen[0].argv == (
        "nanofaas",
        "--endpoint",
        "http://cp:8080",
        "invoke",
        "word-stats",
        "--data",
        '{"text":"a b"}',
    )


@pytest.mark.parametrize("transport", ["http", "cli"])
def test_both_transports_reject_a_200_that_carries_an_error(transport: str) -> None:
    """Curl exits 0 here: without the shared check, both would call it a pass."""
    executor = RecordingExecutor(stdout='{"status":"error","output":"boom"}')
    task = (
        HttpFunctionInvokeTask(
            "fn", payload="{}", endpoint="http://cp:8080", executor=executor, role="host"
        )
        if transport == "http"
        else CliFunctionInvokeTask(
            "fn", payload="{}", cli_argv=("nanofaas",), executor=executor, role="host"
        )
    )

    with pytest.raises(RuntimeError, match="did not report success"):
        task.run(TaskInputs.empty())
