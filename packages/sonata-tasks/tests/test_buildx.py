"""Tests for buildx builder resource."""

from dataclasses import dataclass, field

from sonata_engine import TaskInputs
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.buildx import buildx_builder_resource


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)
    builder_exists: bool = False

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        if task.argv[1:3] == ("buildx", "inspect") and "--bootstrap" not in task.argv:
            rc = 0 if self.builder_exists else 1
            stdout = (
                f"Name: {task.argv[-1]}\n"
                "Platforms: linux/amd64*, linux/arm64*\n"
            ) if self.builder_exists else ""
            return TaskResult(
                task_id="", status="passed", return_code=rc, stdout=stdout,
            )
        return TaskResult(task_id="", status="passed", return_code=0)


def test_buildx_builder_creates_bootstraps_and_removes() -> None:
    executor = RecordingExecutor(builder_exists=False)
    resource = buildx_builder_resource(
        name="release-builder", executor=executor, role="stack",
    )
    state = resource.acquire(TaskInputs.empty())
    assert state == "release-builder"
    resource.release(TaskInputs.empty(), state)

    argv_seqs = [tuple(s.argv) for s in executor.seen]
    assert argv_seqs == [
        ("docker", "buildx", "inspect", "release-builder"),
        ("docker", "buildx", "create", "--name", "release-builder",
         "--driver", "docker-container", "--use"),
        ("docker", "buildx", "inspect", "--bootstrap", "release-builder"),
        ("docker", "buildx", "rm", "--force", "release-builder"),
    ]


def test_buildx_builder_preexisting_is_not_removed() -> None:
    executor = RecordingExecutor(builder_exists=True)
    resource = buildx_builder_resource(
        name="reuse-me", executor=executor, role="stack",
    )
    state = resource.acquire(TaskInputs.empty())
    assert state == "existing"
    resource.release(TaskInputs.empty(), state)
    assert len(executor.seen) == 1  # only inspect, no create or rm
