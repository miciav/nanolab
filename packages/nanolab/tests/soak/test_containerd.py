"""The soak observer uses owned remote containerd reads, never Docker stats."""

import json
from pathlib import Path

import pytest
from sonata_tasks.tasks.models import TaskResult

from nanolab.tasks.containerd_rootless import RootlessRun
from nanolab.tasks.soak.containerd import RootlessCollectionTransport


def test_collection_uses_stack_helper_and_immutable_target() -> None:
    target = {
        "role": "word-stats-java",
        "container_id": "a" * 64,
        "process_id": 123,
        "process_started_at": "456",
        "image_digest": "127.0.0.1:5000/fn@sha256:" + "b" * 64,
        "runtime": "jvm",
        "artifact_kind": "oci-image",
    }

    class Executor:
        def binding_key(self, role: str) -> str:
            return f"test:{role}"

        def __init__(self):
            self.commands = []

        def run(self, task, *, dry_run=False):
            command = task
            self.commands.append(command)
            output = (
                target
                if command.argv[2] == "inspect"
                else {
                    "before": target,
                    "after": target,
                    "cgroup": {
                        "memory_current": 12,
                        "memory_max": 100,
                        "memory_stat": {},
                    },
                }
            )
            return TaskResult(command.task_id, "passed", 0, stdout=json.dumps(output))

    executor = Executor()
    transport = RootlessCollectionTransport(
        RootlessRun(
            "run123",
            Path("/home/ubuntu/nanofaas"),
            Path("/home/ubuntu/nanolab-assets/containerd-rootless/session.sh"),
        ),
        executor,
    )
    inspected, details = transport.inspect("word-stats-java")
    assert details["artifact_kind"] == "oci-image"
    sample = transport.collect(inspected, None, 3)
    assert sample["cgroup"]["memory_current"] == 12
    assert [item.argv[2] for item in executor.commands] == [
        "inspect",
        "inspect",
        "sample",
    ]
    assert all(item.role == "stack" for item in executor.commands)
    assert all("docker" not in " ".join(item.argv) for item in executor.commands)
    assert json.loads(executor.commands[-1].argv[-2])["container_id"] == "a" * 64


def test_collection_rejects_failed_remote_read() -> None:
    class Executor:
        def binding_key(self, role: str) -> str:
            return f"test:{role}"

        def run(self, task, *, dry_run=False):
            command = task
            return TaskResult(command.task_id, "failed", 1, stderr="task missing")

    transport = RootlessCollectionTransport(
        RootlessRun(
            "run123", Path("/home/ubuntu/nanofaas"), Path("/assets/session.sh")
        ),
        Executor(),
    )
    with pytest.raises(OSError, match="task missing"):
        transport.inspect("word-stats-java")
