from pathlib import Path

import pytest
from sonata_engine import JournalConfig, TaskInputs, Workflow, WorkflowTopologyMismatchError
from sonata_tasks.execution.models import TaskResult
from sonata_tasks.testing import RecordingExecutor

from nanolab.tasks.http_function import (
    HttpExecutionSuccessTask,
    HttpFunctionEnqueueTask,
    HttpFunctionRegisterTask,
    HttpFunctionReplicaStatusTask,
)
from nanolab.tasks.manifest import FunctionManifest


def _result(code: int, *, stdout: str = "", stderr: str = "") -> TaskResult:
    return TaskResult(
        task_id="",
        status="passed" if code in {0, 22} else "failed",
        return_code=code,
        expected_exit_codes=frozenset({0, 22}),
        stdout=stdout,
        stderr=stderr,
    )


def _task(executor: RecordingExecutor, **kwargs: object) -> HttpFunctionRegisterTask:
    return HttpFunctionRegisterTask(
        FunctionManifest("word-stats", "registry/word-stats:v1"),
        endpoint="http://control-plane",
        executor=executor,
        role="stack",
        cwd=Path("/workspace"),
        **kwargs,  # type: ignore[arg-type]
    )


def test_register_recovers_a_matching_registration_and_preserves_options() -> None:
    executor = RecordingExecutor(
        results=[
            _result(22, stderr="conflict"),
            _result(
                0,
                stdout=(
                    '{"name":"word-stats","image":"registry/word-stats:v1",'
                    '"requestedExecutionMode":"DEPLOYMENT"}'
                ),
            ),
        ]
    )

    outcome = _task(executor).run(TaskInputs.empty())

    assert outcome.value is not None
    assert outcome.value.return_code == 22
    assert len(executor.seen) == 2
    assert executor.seen[1].argv[-1] == "http://control-plane/v1/functions/word-stats"
    assert executor.seen[1].role == "stack"
    assert executor.seen[1].options.cwd == Path("/workspace")
    assert executor.seen[1].options.expected_exit_codes == frozenset({0})


@pytest.mark.parametrize(
    "second",
    [
        _result(0, stdout="not-json"),
        _result(
            0,
            stdout=(
                '{"name":"word-stats","image":"registry/other:v1",'
                '"requestedExecutionMode":"DEPLOYMENT"}'
            ),
        ),
        _result(7, stderr="unreachable"),
    ],
)
def test_register_does_not_accept_an_unverified_conflict(second: TaskResult) -> None:
    executor = RecordingExecutor(results=[_result(22, stderr="conflict"), second])

    with pytest.raises(RuntimeError, match="conflict"):
        _task(executor).run(TaskInputs.empty())


def test_register_does_not_recover_non_conflict_failures() -> None:
    executor = RecordingExecutor(results=[_result(7, stderr="unreachable")])

    with pytest.raises(RuntimeError, match="unreachable"):
        _task(executor).run(TaskInputs.empty())

    assert len(executor.seen) == 1


def test_register_fingerprint_covers_verification_configuration() -> None:
    first = _task(RecordingExecutor(), expected_backend="kubernetes")
    second = _task(RecordingExecutor(), expected_backend="docker")
    prefix = _task(
        RecordingExecutor(),
        expected_backend="kubernetes",
        expected_endpoint_prefix="http://worker/",
    )

    assert first._fingerprint_payload() != second._fingerprint_payload()
    assert first._fingerprint_payload() != prefix._fingerprint_payload()


def test_register_fingerprint_is_stable_for_reordered_manifest_mappings() -> None:
    one = FunctionManifest(
        "word-stats",
        "registry/word-stats:v1",
        resources={"limits": {"cpu": "1", "memory": "1Gi"}},
    )
    two = FunctionManifest(
        "word-stats",
        "registry/word-stats:v1",
        resources={"limits": {"memory": "1Gi", "cpu": "1"}},
    )
    first = HttpFunctionRegisterTask(
        one,
        endpoint="http://control-plane",
        executor=RecordingExecutor(target_key="stack"),
        role="stack",
    )
    second = HttpFunctionRegisterTask(
        two,
        endpoint="http://control-plane",
        executor=RecordingExecutor(target_key="stack"),
        role="stack",
    )

    assert first._fingerprint_payload() == second._fingerprint_payload()


def test_polling_and_enqueue_fingerprints_cover_semantic_configuration() -> None:
    executor = RecordingExecutor(target_key="stack")
    status = HttpFunctionReplicaStatusTask(
        "word-stats",
        replicas=2,
        endpoint="http://control-plane",
        executor=executor,
        role="stack",
        timeout_seconds=10,
    )
    changed_status = HttpFunctionReplicaStatusTask(
        "word-stats",
        replicas=3,
        endpoint="http://control-plane",
        executor=executor,
        role="stack",
        timeout_seconds=10,
    )
    enqueue = HttpFunctionEnqueueTask(
        "word-stats",
        payload='{"text":"one"}',
        endpoint="http://control-plane",
        executor=executor,
        role="stack",
        idempotency_key="request-1",
    )
    changed_enqueue = HttpFunctionEnqueueTask(
        "word-stats",
        payload='{"text":"two"}',
        endpoint="http://control-plane",
        executor=executor,
        role="stack",
        idempotency_key="request-1",
    )
    success = HttpExecutionSuccessTask(
        endpoint="http://control-plane",
        executor=executor,
        role="stack",
        expected_status_code=200,
    )
    changed_success = HttpExecutionSuccessTask(
        endpoint="http://control-plane",
        executor=executor,
        role="stack",
        expected_status_code=202,
    )

    assert status._fingerprint_payload() != changed_status._fingerprint_payload()
    assert enqueue._fingerprint_payload() != changed_enqueue._fingerprint_payload()
    assert success._fingerprint_payload() != changed_success._fingerprint_payload()
    assert "request-1" not in repr(enqueue._fingerprint_payload())


def test_journal_resume_accepts_same_polling_config_and_rejects_changed_config(
    tmp_path: Path,
) -> None:
    journal = JournalConfig(tmp_path / "replicas.jsonl")

    def workflow(replicas: int, executor: RecordingExecutor) -> Workflow:
        result = Workflow("replica-status")
        result.add(
            HttpFunctionReplicaStatusTask(
                "word-stats",
                replicas=replicas,
                endpoint="http://control-plane",
                executor=executor,
                role="stack",
                timeout_seconds=1,
                poll_seconds=0.01,
            )
        )
        return result

    matching = _result(0, stdout='{"desiredReplicas":2,"readyReplicas":2}')
    workflow(2, RecordingExecutor(target_key="stack", results=[matching])).run(
        journal=journal
    )
    workflow(2, RecordingExecutor(target_key="stack", results=[matching])).run(
        journal=journal, resume=True
    )

    with pytest.raises(WorkflowTopologyMismatchError):
        workflow(3, RecordingExecutor(target_key="stack")).run(
            journal=journal, resume=True
        )
