from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.cli_function import CliFunctionApplyTask, CliFunctionDeleteTask
from sonata_tasks.http_function import (
    HttpExecutionSuccessTask,
    HttpFunctionDeleteTask,
    HttpFunctionEnqueueTask,
    HttpFunctionRegisterTask,
)
from sonata_tasks.manifest import FunctionManifest

MANIFEST = FunctionManifest(name="word-stats", image="reg/word-stats:e2e")
CLI_ARGV = ("nanofaas-cli", "--endpoint", "http://127.0.0.1:8080", "--namespace", "nf")


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0)


@dataclass
class RespondingExecutor(RecordingExecutor):
    stdout: str = ""

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=self.stdout)


@dataclass
class SequencedExecutor(RecordingExecutor):
    responses: list[str] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(
            task_id="",
            status="passed",
            return_code=0,
            stdout=self.responses.pop(0),
        )


def test_manifest_carries_the_tuning_values_the_control_plane_expects() -> None:
    assert MANIFEST.body() == {
        "name": "word-stats",
        "image": "reg/word-stats:e2e",
        "executionMode": "DEPLOYMENT",
        "timeoutMs": 5000,
        "concurrency": 2,
        "queueSize": 20,
        "maxRetries": 3,
    }


def test_manifest_omits_the_optional_halves_when_unset() -> None:
    assert "resources" not in MANIFEST.body()
    assert "scalingConfig" not in MANIFEST.body()


def test_manifest_includes_them_when_given() -> None:
    body = FunctionManifest(
        name="f",
        image="i",
        resources={"limits": {"memoryMiB": 512}},
        scaling_config={"strategy": "INTERNAL"},
    ).body()

    assert body["resources"] == {"limits": {"memoryMiB": 512}}
    assert body["scalingConfig"] == {"strategy": "INTERNAL"}


def test_manifest_json_is_compact_so_it_survives_a_shell_word() -> None:
    assert ", " not in MANIFEST.json()
    assert json.loads(MANIFEST.json())["name"] == "word-stats"


def test_cli_apply_writes_the_manifest_on_the_target_not_here() -> None:
    executor = RecordingExecutor()

    task = CliFunctionApplyTask(
        MANIFEST, cli_argv=CLI_ARGV, executor=executor, role="stack"
    )
    _ = task.run(TaskInputs.empty())

    script = executor.seen[0].argv[-1]
    assert task.title == "Apply word-stats"
    assert executor.seen[0].argv[:2] == ("bash", "-lc")
    assert "mktemp" in script
    assert "reg/word-stats:e2e" in script
    assert "fn apply --file" in script.replace("'", "")


def test_cli_delete_accepts_only_a_clean_exit() -> None:
    """The CLI already exits 0 when the function is absent, so tolerating 1 would
    hide real cleanup failures."""
    executor = RecordingExecutor()

    task = CliFunctionDeleteTask(
        "word-stats", cli_argv=CLI_ARGV, executor=executor, role="stack"
    )
    _ = task.run(TaskInputs.empty())

    assert executor.seen[0].argv == (*CLI_ARGV, "fn", "delete", "word-stats")
    assert executor.seen[0].expected_exit_codes == frozenset({0})


def test_http_register_posts_the_same_manifest() -> None:
    executor = RecordingExecutor()

    task = HttpFunctionRegisterTask(
        MANIFEST, endpoint="http://cp:8080", executor=executor, role="host"
    )
    _ = task.run(TaskInputs.empty())

    argv = executor.seen[0].argv
    assert task.title == "Register word-stats"
    assert argv[0] == "curl"
    assert argv[-1] == "http://cp:8080/v1/functions"
    assert MANIFEST.json() in argv


def test_k8s_register_requires_the_provider_derived_endpoint() -> None:
    executor = RespondingExecutor(
        stdout=json.dumps(
            {
                "name": "word-stats",
                "image": "reg/word-stats:e2e",
                "requestedExecutionMode": "DEPLOYMENT",
                "effectiveExecutionMode": "DEPLOYMENT",
                "deploymentBackend": "k8s",
                "endpointUrl": "http://fn-word-stats.nf.svc.cluster.local:8080/invoke",
            }
        )
    )

    _ = HttpFunctionRegisterTask(
        MANIFEST,
        endpoint="http://cp:8080",
        executor=executor,
        role="stack",
        expected_backend="k8s",
        expected_endpoint_prefix="http://fn-word-stats.nf.svc.cluster.local:8080/invoke",
    ).run(TaskInputs.empty())


def test_k8s_register_rejects_a_non_provider_endpoint() -> None:
    executor = RespondingExecutor(
        stdout=json.dumps(
            {
                "name": "word-stats",
                "image": "reg/word-stats:e2e",
                "requestedExecutionMode": "DEPLOYMENT",
                "effectiveExecutionMode": "DEPLOYMENT",
                "deploymentBackend": "k8s",
                "endpointUrl": "http://wrong.example/invoke",
            }
        )
    )

    task = HttpFunctionRegisterTask(
        MANIFEST,
        endpoint="http://cp:8080",
        executor=executor,
        role="stack",
        expected_backend="k8s",
        expected_endpoint_prefix="http://fn-word-stats.nf.svc.cluster.local:8080/invoke",
    )

    with pytest.raises(RuntimeError, match="endpointUrl"):
        _ = task.run(TaskInputs.empty())


def test_enqueue_returns_the_execution_id_and_checks_idempotency() -> None:
    executor = SequencedExecutor(
        responses=[
            '{"executionId":"e-1","status":"queued"}',
            '{"executionId":"e-1","status":"queued"}',
        ]
    )
    first = HttpFunctionEnqueueTask(
        "word-stats",
        payload='{"input":{"text":"a b"}}',
        endpoint="http://cp:8080",
        executor=executor,
        role="stack",
        idempotency_key="same-request",
    ).run(TaskInputs.empty())
    second = HttpFunctionEnqueueTask(
        "word-stats",
        payload='{"input":{"text":"a b"}}',
        endpoint="http://cp:8080",
        executor=executor,
        role="stack",
        idempotency_key="same-request",
        match_upstream=True,
    ).run(_with_upstream(first.value))

    assert first.value == second.value == "e-1"
    assert "Idempotency-Key: same-request" in executor.seen[0].argv


def test_poll_waits_for_a_successful_execution() -> None:
    executor = SequencedExecutor(
        responses=[
            '{"executionId":"e-1","status":"queued"}',
            '{"executionId":"e-1","status":"success"}',
        ]
    )

    _ = HttpExecutionSuccessTask(
        endpoint="http://cp:8080",
        executor=executor,
        role="stack",
        poll_seconds=0,
    ).run(_with_upstream("e-1"))

    assert executor.seen[-1].argv[-1] == "http://cp:8080/v1/executions/e-1"


def _with_upstream(value: object) -> TaskInputs:
    from dataclasses import replace

    return replace(TaskInputs.empty(), _upstream=value)


def test_http_delete_tolerates_an_unreachable_or_absent_target() -> None:
    executor = RecordingExecutor()

    task = HttpFunctionDeleteTask(
        "word-stats", endpoint="http://cp:8080", executor=executor, role="host"
    )
    _ = task.run(TaskInputs.empty())

    assert executor.seen[0].argv[-1] == "http://cp:8080/v1/functions/word-stats"
    # 7 is "could not connect", 22 is "HTTP error": the control plane may already
    # be gone when cleanup runs.
    assert executor.seen[0].expected_exit_codes == frozenset({0, 7, 22})


def test_a_remote_role_still_gets_a_plain_argv() -> None:
    """No shell wrapper, even for a VM: every executor quotes the argv it is
    handed — multipass with shlex.join, proxmox with shlex.quote per argument,
    azure structurally. The task pre-quoting as well would be a second round for
    nobody, and it was only ever there to carry a `$(...)` the endpoint no longer
    needs."""
    executor = RecordingExecutor()

    _ = HttpFunctionRegisterTask(
        MANIFEST, endpoint="http://cp:8080", executor=executor, role="stack"
    ).run(TaskInputs.empty())

    argv = executor.seen[0].argv
    assert argv[0] == "curl"
    assert MANIFEST.json() in argv
    assert executor.seen[0].execution_role == "stack"


def test_both_transports_send_a_byte_identical_manifest() -> None:
    """The body used to be built twice, with the CLI copy hard-coding its values."""
    cli_executor = RecordingExecutor()
    http_executor = RecordingExecutor()

    _ = CliFunctionApplyTask(
        MANIFEST, cli_argv=CLI_ARGV, executor=cli_executor, role="host"
    ).run(TaskInputs.empty())
    _ = HttpFunctionRegisterTask(
        MANIFEST, endpoint="http://cp:8080", executor=http_executor, role="host"
    ).run(TaskInputs.empty())

    assert MANIFEST.json() in cli_executor.seen[0].argv[-1]
    assert MANIFEST.json() in http_executor.seen[0].argv
