from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.cosign import COSIGN_IMAGE, CosignTask


def test_cosign_image_is_digest_pinned() -> None:
    # A mutable tag like `:latest` would let the supply-chain toolchain drift
    # out from under a pinned release. Assert the shape -- a `@sha256:` digest
    # followed by 64 hex characters -- not today's specific digest, so a
    # legitimate pin bump doesn't fail this test.
    assert re.search(r"@sha256:[0-9a-f]{64}$", COSIGN_IMAGE)
    assert ":latest" not in COSIGN_IMAGE


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


def _run(task: CosignTask) -> None:
    task.run(TaskInputs.empty())


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
            "--yes",
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
        assert argv[-9:] == (
            "attest",
            "--yes",
            "--key",
            "/key.cosign",
            "--type",
            "custom",
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
        assert argv[-7:] == (
            "attach",
            "sbom",
            "--sbom",
            "/sbom.json",
            "--type",
            "spdx",
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
        assert argv[-6:] == (
            "verify-attestation",
            "--key",
            "/pub.key",
            "--type",
            "custom",
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


def test_sign_does_not_wait_for_confirmation() -> None:
    executor = RecordingExecutor()
    _run(CosignTask(
        operation="sign", image="img@sha256:aa", key_file="/secrets/cosign.key",
        password_file="/secrets/pw", docker_config="/home/user/.docker",
        executor=executor, role="stack",
    ))
    argv = executor.seen[0].argv
    assert "--yes" in argv


def test_attest_declares_the_custom_predicate_type() -> None:
    executor = RecordingExecutor()
    _run(CosignTask(
        operation="attest", image="img@sha256:aa", key_file="/secrets/cosign.key",
        password_file="/secrets/pw", docker_config="/home/user/.docker",
        predicate_file="/work/predicate.json", executor=executor, role="stack",
    ))
    argv = executor.seen[0].argv
    assert "--yes" in argv
    assert argv[argv.index("--type") + 1] == "custom"


def test_attach_sbom_declares_spdx() -> None:
    executor = RecordingExecutor()
    _run(CosignTask(
        operation="attach sbom", image="img@sha256:aa", key_file="/secrets/cosign.key",
        password_file="/secrets/pw", docker_config="/home/user/.docker",
        sbom_file="/work/sbom.spdx.json", executor=executor, role="stack",
    ))
    argv = executor.seen[0].argv
    assert argv[argv.index("--type") + 1] == "spdx"


def test_verify_attestation_declares_the_custom_predicate_type() -> None:
    executor = RecordingExecutor()
    _run(CosignTask(
        operation="verify-attestation", image="img@sha256:aa",
        key_file="/secrets/cosign.key", password_file="/secrets/pw",
        docker_config="/home/user/.docker", public_key_file="/work/cosign.pub",
        executor=executor, role="stack",
    ))
    argv = executor.seen[0].argv
    assert argv[argv.index("--type") + 1] == "custom"


def test_public_key_writes_the_derived_key_to_a_file() -> None:
    executor = RecordingExecutor()
    _run(CosignTask(
        operation="public-key", image="", key_file="/secrets/cosign.key",
        password_file="/secrets/pw", docker_config="/home/user/.docker",
        output_file="/work/cosign.pub", executor=executor, role="stack",
    ))
    argv = executor.seen[0].argv
    assert "public-key" in argv
    assert "/work/cosign.pub" in " ".join(argv)


def test_public_key_output_file_is_not_interpolated_into_the_shell_script() -> None:
    # output_file must reach the wrapper as a positional shell parameter, not
    # be formatted into the `-c` script text -- otherwise a value containing
    # a quote, `$`, or a backtick breaks out of the intended redirect.
    executor = RecordingExecutor()
    dangerous = '/work/cosign.pub"; rm -rf /; echo "pwned'
    _run(CosignTask(
        operation="public-key", image="", key_file="/secrets/cosign.key",
        password_file="/secrets/pw", docker_config="/home/user/.docker",
        output_file=dangerous, executor=executor, role="stack",
    ))
    argv = executor.seen[0].argv
    script = argv[2]
    assert dangerous not in script
    assert dangerous in argv
