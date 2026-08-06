from dataclasses import dataclass, field

from sonata_engine import TaskInputs
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.registry import docker_registry_resource


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(
            task_id="",
            status="passed",
            return_code=1 if task.argv[1] == "inspect" else 0,
        )


def test_registry_resource_creates_and_removes_only_its_container() -> None:
    executor = RecordingExecutor()
    resource = docker_registry_resource(
        executor=executor,
        role="host",
        ready=lambda: True,
    )

    state = resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), state)

    assert state == "created"
    assert [task.argv[:2] for task in executor.seen] == [
        ("docker", "inspect"),
        ("docker", "run"),
        ("docker", "rm"),
    ]
