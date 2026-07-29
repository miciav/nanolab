from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.cosign import COSIGN_IMAGE, CosignTask


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0)


@dataclass
class FailingExecutor:
    stderr: str = "cosign: exit code 1"

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        return TaskResult(
            task_id="",
            status="failed",
            return_code=1,
            stderr=self.stderr,
        )


COSIGN_ARGS = (
    "docker",
    "run",
    "--rm",
    "--user",
    "0",
    "-e",
    "COSIGN_PASSWORD",
    "-e",
    "DOCKER_CONFIG=/auth",
    "-v",
    "/home/user/.docker:/auth:ro",
    "-v",
    "/secrets/cosign.key:/key.cosign:ro",
    COSIGN_IMAGE,
)


class TestCosignTask:
    def test_sign_operation(self) -> None:
        executor = RecordingExecutor()
        task = CosignTask(
            operation="sign",
            image="reg/app:1.0.0",
            key_file="/secrets/cosign.key",
            password_file="/secrets/cosign.password",
            docker_config="/home/user/.docker",
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())

        assert task.title == "cosign sign reg/app:1.0.0"
        assert executor.seen[0].argv == (
            "sh",
            "-c",
            'pw=$(cat "$1"); shift; COSIGN_PASSWORD="$pw" exec "$@"',
            "--",
            "/secrets/cosign.password",
            *COSIGN_ARGS,
            "sign",
            "--key",
            "/key.cosign",
            "reg/app:1.0.0",
        )

    def test_attest_operation(self) -> None:
        executor = RecordingExecutor()
        task = CosignTask(
            operation="attest",
            image="reg/app:1.0.0",
            key_file="/secrets/cosign.key",
            password_file="/secrets/cosign.password",
            docker_config="/home/user/.docker",
            executor=executor,
            role="host",
            predicate_file="/tmp/predicate.json",
        )
        task.run(TaskInputs.empty())

        assert task.title == "cosign attest reg/app:1.0.0"
        argv = executor.seen[0].argv
        assert "-v" in argv
        assert "/tmp/predicate.json:/predicate.json:ro" in argv
        assert argv[-6:] == (
            "attest",
            "--key",
            "/key.cosign",
            "--predicate",
            "/predicate.json",
            "reg/app:1.0.0",
        )

    def test_attach_sbom_operation(self) -> None:
        executor = RecordingExecutor()
        task = CosignTask(
            operation="attach sbom",
            image="reg/app:1.0.0",
            key_file="/secrets/cosign.key",
            password_file="/secrets/cosign.password",
            docker_config="/home/user/.docker",
            executor=executor,
            role="host",
            sbom_file="/tmp/sbom.spdx.json",
        )
        task.run(TaskInputs.empty())

        assert task.title == "cosign attach sbom reg/app:1.0.0"
        argv = executor.seen[0].argv
        assert "-v" in argv
        assert "/tmp/sbom.spdx.json:/sbom.json:ro" in argv
        assert argv[-5:] == (
            "attach",
            "sbom",
            "--sbom",
            "/sbom.json",
            "reg/app:1.0.0",
        )

    def test_verify_operation(self) -> None:
        executor = RecordingExecutor()
        task = CosignTask(
            operation="verify",
            image="reg/app:1.0.0",
            key_file="/secrets/cosign.key",
            password_file="/secrets/cosign.password",
            docker_config="/home/user/.docker",
            executor=executor,
            role="host",
            public_key_file="/secrets/cosign.pub",
        )
        task.run(TaskInputs.empty())

        assert task.title == "cosign verify reg/app:1.0.0"
        argv = executor.seen[0].argv
        assert "-v" in argv
        assert "/secrets/cosign.pub:/pub.key:ro" in argv
        assert argv[-4:] == (
            "verify",
            "--key",
            "/pub.key",
            "reg/app:1.0.0",
        )

    def test_verify_attestation_operation(self) -> None:
        executor = RecordingExecutor()
        task = CosignTask(
            operation="verify-attestation",
            image="reg/app:1.0.0",
            key_file="/secrets/cosign.key",
            password_file="/secrets/cosign.password",
            docker_config="/home/user/.docker",
            executor=executor,
            role="host",
            public_key_file="/secrets/cosign.pub",
        )
        task.run(TaskInputs.empty())

        assert task.title == "cosign verify-attestation reg/app:1.0.0"
        argv = executor.seen[0].argv
        assert argv[-4:] == (
            "verify-attestation",
            "--key",
            "/pub.key",
            "reg/app:1.0.0",
        )

    def test_unknown_operation_raises(self) -> None:
        executor = RecordingExecutor()
        with pytest.raises(ValueError, match="unknown cosign operation"):
            CosignTask(
                operation="bogus",
                image="reg/app:1.0.0",
                key_file="/secrets/cosign.key",
                password_file="/secrets/cosign.password",
                docker_config="/home/user/.docker",
                executor=executor,
                role="host",
            )

    def test_fails_on_execution_error(self) -> None:
        executor = FailingExecutor(stderr="verification failed")
        task = CosignTask(
            operation="verify",
            image="reg/app:1.0.0",
            key_file="/secrets/cosign.key",
            password_file="/secrets/cosign.password",
            docker_config="/home/user/.docker",
            executor=executor,
            role="host",
        )
        with pytest.raises(RuntimeError, match="verification failed"):
            task.run(TaskInputs.empty())

    def test_is_a_command_task(self) -> None:
        executor = RecordingExecutor()
        assert isinstance(
            CosignTask(
                operation="sign",
                image="reg/app:1.0.0",
                key_file="/secrets/cosign.key",
                password_file="/secrets/cosign.password",
                docker_config="/home/user/.docker",
                executor=executor,
                role="host",
            ),
            CommandTask,
        )
