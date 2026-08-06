from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.skopeo import SkopeoCopyTask, SkopeoInspectTask


@dataclass
class RecordingExecutor:
    stdout: str = ""
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(
            task_id="", status="passed", return_code=0, stdout=self.stdout
        )


@dataclass
class FailingExecutor:
    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        return TaskResult(
            task_id="",
            status="failed",
            return_code=1,
            stderr="unauthorized: authentication required",
        )


class TestSkopeoCopyTask:
    def test_argv_with_src_tls_verify_disabled(self) -> None:
        executor = RecordingExecutor()
        task = SkopeoCopyTask(
            source="reg.ecr.us-east-1.amazonaws.com/app:1.0.0",
            destination="localhost:5000/app:1.0.0",
            authfile="/home/user/.docker/config.json",
            src_tls_verify=False,
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())

        assert task.title == "Copy image reg.ecr.us-east-1.amazonaws.com/app:1.0.0 -> localhost:5000/app:1.0.0"
        assert executor.seen[0].argv == (
            "skopeo",
            "copy",
            "--preserve-digests",
            "--src-tls-verify=false",
            "--dest-authfile",
            "/home/user/.docker/config.json",
            "docker://reg.ecr.us-east-1.amazonaws.com/app:1.0.0",
            "docker://localhost:5000/app:1.0.0",
        )

    def test_argv_with_tls_verify_by_default(self) -> None:
        """When src_tls_verify is omitted (default True), no --src-tls-verify flag."""
        executor = RecordingExecutor()
        task = SkopeoCopyTask(
            source="reg/source:tag",
            destination="reg/dest:tag",
            authfile="/auth.json",
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())
        assert "--src-tls-verify=false" not in executor.seen[0].argv

    def test_fails_on_bad_auth(self) -> None:
        executor = FailingExecutor()
        task = SkopeoCopyTask(
            source="s",
            destination="d",
            authfile="bad-auth",
            src_tls_verify=False,
            executor=executor,
            role="host",
        )
        with pytest.raises(RuntimeError, match="unauthorized"):
            task.run(TaskInputs.empty())


class TestSkopeoInspectTask:
    def test_argv_with_tls_verify_disabled(self) -> None:
        executor = RecordingExecutor(stdout="sha256:abc123")
        task = SkopeoInspectTask(
            reference="reg.ecr.us-east-1.amazonaws.com/app:1.0.0",
            authfile="/home/user/.docker/config.json",
            tls_verify=False,
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())

        assert task.title == "Inspect reg.ecr.us-east-1.amazonaws.com/app:1.0.0"
        assert executor.seen[0].argv == (
            "skopeo",
            "inspect",
            "--format={{.Digest}}",
            "--authfile",
            "/home/user/.docker/config.json",
            "--tls-verify=false",
            "docker://reg.ecr.us-east-1.amazonaws.com/app:1.0.0",
        )

    def test_argv_with_tls_verify_by_default(self) -> None:
        """When tls_verify is omitted (default True), no --tls-verify flag."""
        executor = RecordingExecutor(stdout="sha256:abc123")
        task = SkopeoInspectTask(
            reference="reg/repo:tag",
            authfile="/auth.json",
            executor=executor,
            role="host",
        )
        task.run(TaskInputs.empty())
        assert "--tls-verify=false" not in executor.seen[0].argv

    def test_returns_the_digest(self) -> None:
        executor = RecordingExecutor(stdout="sha256:def456")
        task = SkopeoInspectTask(
            reference="reg/repo:tag",
            authfile="/auth.json",
            executor=executor,
            role="host",
        )
        result = task.run(TaskInputs.empty())
        assert result.value.stdout == "sha256:def456"

    def test_fails_on_bad_auth(self) -> None:
        executor = FailingExecutor()
        task = SkopeoInspectTask(
            reference="reg/repo:tag",
            authfile="bad-auth",
            tls_verify=False,
            executor=executor,
            role="host",
        )
        with pytest.raises(RuntimeError, match="unauthorized"):
            task.run(TaskInputs.empty())


class TestSkopeoTaskHierarchy:
    def test_they_are_command_tasks(self) -> None:
        executor = RecordingExecutor()
        assert isinstance(
            SkopeoCopyTask(
                source="s",
                destination="d",
                authfile="a",
                src_tls_verify=False,
                executor=executor,
                role="host",
            ),
            CommandTask,
        )
        assert isinstance(
            SkopeoInspectTask(
                reference="r",
                authfile="a",
                tls_verify=False,
                executor=executor,
                role="host",
            ),
            CommandTask,
        )
