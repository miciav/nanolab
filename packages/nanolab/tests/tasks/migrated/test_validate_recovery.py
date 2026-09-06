from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
import json
from pathlib import Path

import pytest
from sonata_engine import TaskInputs
from nanolab.tasks.compose import DockerComposeProject
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult


@dataclass
class SequencedExecutor:
    responses: list[str]
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def binding_key(self, role: str) -> str:
        return f"test:{role}"

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(
            task_id="", status="passed", return_code=0, stdout=self.responses.pop(0)
        )


def _recovery_module():
    return import_module("nanolab.tasks.validate_recovery")


def test_managed_container_ids_require_exactly_two_running_instances() -> None:
    module = _recovery_module()
    executor = SequencedExecutor(responses=["first\nsecond\n"])

    outcome = module.ManagedContainerIdsTask(
        "word-stats-java", executor=executor, role="host"
    ).run(TaskInputs.empty())

    assert outcome.value == ("first", "second")
    assert executor.seen[0].argv == (
        "docker",
        "ps",
        "-q",
        "--filter",
        "label=io.nanofaas.managed=true",
        "--filter",
        "label=io.nanofaas.function=word-stats-java",
    )


@pytest.mark.parametrize("ids", ["", "only\n", "one\ntwo\nthree\n"])
def test_managed_container_ids_reject_an_unexpected_count(ids: str) -> None:
    module = _recovery_module()

    with pytest.raises(RuntimeError, match="expected exactly 2 running managed containers"):
        _ = module.ManagedContainerIdsTask(
            "word-stats-java", executor=SequencedExecutor(responses=[ids]), role="host"
        ).run(TaskInputs.empty())


def test_orphan_cleanup_skips_docker_rm_when_no_container_is_left() -> None:
    module = _recovery_module()
    executor = SequencedExecutor(responses=[""])

    _ = module.RemoveManagedContainersTask(
        ("word-stats-java",), executor=executor, role="host"
    ).run(TaskInputs.empty())

    assert len(executor.seen) == 1


def test_container_recovery_restarts_only_the_control_plane_and_keeps_ids() -> None:
    module = _recovery_module()
    executor = SequencedExecutor(
        responses=[
            "{}",
            '{"desiredReplicas":2,"readyReplicas":2}',
            "first\nsecond\n",
            "",
            "{}",
            '{"deploymentBackend":"container-local"}',
            '{"desiredReplicas":2,"readyReplicas":2}',
            "first\nsecond\n",
            '{"status":"success","output":"ok"}',
        ]
    )
    project = DockerComposeProject(
        name="nanofaas-recovery",
        file=Path("deploy/compose/compose.yaml"),
        ready_url="http://127.0.0.1:8081/actuator/health/readiness",
    )

    _ = module.ContainerPersistentRecoveryTask(
        name="word-stats-java",
        payload='{"input":{"text":"a b"}}',
        project=project,
        endpoint="http://127.0.0.1:8080",
        executor=executor,
        role="host",
    ).run(TaskInputs.empty())

    assert ("docker", "compose", "-f", "deploy/compose/compose.yaml", "-p", "nanofaas-recovery", "restart", "control-plane") in [
        task.argv for task in executor.seen
    ]
    assert [task.argv for task in executor.seen if task.argv[:2] == ("docker", "ps")] == [
        executor.seen[2].argv,
        executor.seen[7].argv,
    ]


def _pod(name: str, uid: str, *, ready: bool = True) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": name, "uid": uid},
                    "status": {"conditions": [{"type": "Ready", "status": str(ready)}]},
                }
            ]
        }
    )


def test_kubernetes_recovery_replaces_only_the_control_plane_pod() -> None:
    module = _recovery_module()
    executor = SequencedExecutor(
        responses=[
            "{}",
            '{"desiredReplicas":2,"readyReplicas":2}',
            "deployment-uid",
            "service-uid",
            _pod("control-plane-old", "old-uid"),
            "",
            _pod("control-plane-new", "new-uid"),
            '{"deploymentBackend":"k8s"}',
            '{"desiredReplicas":2,"readyReplicas":2}',
            "deployment-uid",
            "service-uid",
            '{"status":"success","output":"ok"}',
        ]
    )

    _ = module.KubernetesPersistentRecoveryTask(
        name="word-stats-java",
        payload='{"input":{"text":"a b"}}',
        namespace="nanofaas",
        endpoint="http://10.43.0.7:8080",
        executor=executor,
        role="stack",
        poll_seconds=0,
    ).run(TaskInputs.empty())

    assert executor.seen[4].argv == (
        "kubectl",
        "-n",
        "nanofaas",
        "get",
        "pods",
        "-l",
        "app=nanofaas-control-plane",
        "-o",
        "json",
    )
    assert executor.seen[5].argv == (
        "kubectl",
        "-n",
        "nanofaas",
        "delete",
        "pod",
        "control-plane-old",
        "--wait=true",
    )


def test_kubernetes_recovery_rejects_recreated_function_resources() -> None:
    module = _recovery_module()
    executor = SequencedExecutor(
        responses=[
            "{}",
            '{"desiredReplicas":2,"readyReplicas":2}',
            "deployment-uid",
            "service-uid",
            _pod("control-plane-old", "old-uid"),
            "",
            _pod("control-plane-new", "new-uid"),
            '{"deploymentBackend":"k8s"}',
            '{"desiredReplicas":2,"readyReplicas":2}',
            "new-deployment-uid",
            "service-uid",
        ]
    )

    with pytest.raises(RuntimeError, match="function resource UIDs changed"):
        _ = module.KubernetesPersistentRecoveryTask(
            name="word-stats-java",
            payload='{"input":{"text":"a b"}}',
            namespace="nanofaas",
            endpoint="http://10.43.0.7:8080",
            executor=executor,
            role="stack",
            poll_seconds=0,
        ).run(TaskInputs.empty())
