from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.resources import ContainerResourceCheckTask, K8sResourceCheckTask

SPEC = {
    "requests": {"cpu": 0.25, "memoryMiB": 256},
    "limits": {"cpu": 1.0, "memoryMiB": 512},
}


@dataclass
class StubExecutor:
    stdout: str = ""
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=self.stdout)


def _host_config(cpu_shares: int, nano_cpus: int, reservation: int, memory: int) -> str:
    return json.dumps(
        {
            "CpuShares": cpu_shares,
            "NanoCpus": nano_cpus,
            "MemoryReservation": reservation,
            "Memory": memory,
        }
    )


# 0.25 * 1024 + 0.5 -> 256; 1.0 * 1e9; request != limit so reservation is 256MiB; limit 512MiB
MATCHING_HOST_CONFIG = _host_config(256, 1_000_000_000, 256 * 1024 * 1024, 512 * 1024 * 1024)


def _k8s_payload(req_cpu: str, req_mem: str, lim_cpu: str, lim_mem: str) -> str:
    return json.dumps(
        {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "resources": {
                                    "requests": {"cpu": req_cpu, "memory": req_mem},
                                    "limits": {"cpu": lim_cpu, "memory": lim_mem},
                                }
                            }
                        ]
                    }
                }
            }
        }
    )


MATCHING_K8S = _k8s_payload("250m", "256Mi", "1", "512Mi")


def test_container_check_reads_the_host_config_as_json() -> None:
    executor = StubExecutor(stdout=MATCHING_HOST_CONFIG)

    ContainerResourceCheckTask(
        container="nanofaas-word-stats-r1", resources=SPEC, executor=executor, role="host"
    ).run(TaskInputs.empty())

    assert executor.seen[0].argv == (
        "docker",
        "inspect",
        "--format={{json .HostConfig}}",
        "nanofaas-word-stats-r1",
    )


def test_container_check_passes_when_every_field_matches() -> None:
    executor = StubExecutor(stdout=MATCHING_HOST_CONFIG)

    ContainerResourceCheckTask(
        container="nanofaas-word-stats-r1", resources=SPEC, executor=executor, role="host"
    ).run(TaskInputs.empty())


@pytest.mark.parametrize(
    ("stdout", "expected_field"),
    [
        (_host_config(1, 1_000_000_000, 256 * 1024 * 1024, 512 * 1024 * 1024), "CpuShares"),
        (_host_config(256, 5, 256 * 1024 * 1024, 512 * 1024 * 1024), "NanoCpus"),
        (_host_config(256, 1_000_000_000, 7, 512 * 1024 * 1024), "MemoryReservation"),
        (_host_config(256, 1_000_000_000, 256 * 1024 * 1024, 9), "Memory"),
    ],
)
def test_container_check_names_the_field_that_mismatched(stdout: str, expected_field: str) -> None:
    """The shell version compared one space-joined line, so a failure never said which."""
    executor = StubExecutor(stdout=stdout)
    task = ContainerResourceCheckTask(
        container="nanofaas-word-stats-r1", resources=SPEC, executor=executor, role="host"
    )

    with pytest.raises(RuntimeError, match=expected_field):
        task.run(TaskInputs.empty())


def test_container_reservation_is_zero_when_request_equals_limit() -> None:
    same = {"requests": {"cpu": 1.0, "memoryMiB": 512}, "limits": {"cpu": 1.0, "memoryMiB": 512}}
    executor = StubExecutor(
        stdout=_host_config(1024, 1_000_000_000, 0, 512 * 1024 * 1024)
    )

    ContainerResourceCheckTask(
        container="nanofaas-word-stats-r1", resources=same, executor=executor, role="host"
    ).run(TaskInputs.empty())


def test_container_cpu_shares_never_drop_below_two() -> None:
    tiny = {"requests": {"cpu": 0.001, "memoryMiB": 8}, "limits": {"cpu": 0.002, "memoryMiB": 8}}
    executor = StubExecutor(stdout=_host_config(2, 2_000_000, 0, 8 * 1024 * 1024))

    ContainerResourceCheckTask(
        container="nanofaas-word-stats-r1", resources=tiny, executor=executor, role="host"
    ).run(TaskInputs.empty())


def test_container_check_without_a_spec_only_reads() -> None:
    executor = StubExecutor(stdout="not json at all")

    ContainerResourceCheckTask(
        container="nanofaas-word-stats-r1", resources=None, executor=executor, role="host"
    ).run(TaskInputs.empty())


def test_k8s_check_reads_the_deployment_as_json() -> None:
    executor = StubExecutor(stdout=MATCHING_K8S)

    K8sResourceCheckTask(
        deployment="fn-word-stats", namespace="research", resources=SPEC, executor=executor, role="stack"
    ).run(TaskInputs.empty())

    # The namespace sits right after kubectl, where KubectlTask puts it for every
    # caller, rather than wherever each call site chose to append it.
    assert executor.seen[0].argv == (
        "kubectl",
        "-n",
        "research",
        "get",
        "deployment",
        "fn-word-stats",
        "-o=json",
    )


@pytest.mark.parametrize(
    ("payload", "expected_field"),
    [
        (_k8s_payload("1", "256Mi", "1", "512Mi"), "requests.cpu"),
        (_k8s_payload("250m", "1Mi", "1", "512Mi"), "requests.memory"),
        (_k8s_payload("250m", "256Mi", "250m", "512Mi"), "limits.cpu"),
        (_k8s_payload("250m", "256Mi", "1", "1Mi"), "limits.memory"),
    ],
)
def test_k8s_check_names_the_field_that_mismatched(payload: str, expected_field: str) -> None:
    executor = StubExecutor(stdout=payload)
    task = K8sResourceCheckTask(
        deployment="fn-word-stats", namespace="research", resources=SPEC, executor=executor, role="stack"
    )

    with pytest.raises(RuntimeError, match=expected_field):
        task.run(TaskInputs.empty())


def test_k8s_whole_cpu_is_not_rendered_in_millicores() -> None:
    """`1` and `1000m` are the same quantity, but the legacy check compared strings."""
    executor = StubExecutor(stdout=MATCHING_K8S)

    K8sResourceCheckTask(
        deployment="fn-word-stats", namespace="research", resources=SPEC, executor=executor, role="stack"
    ).run(TaskInputs.empty())


def test_malformed_output_is_reported_as_such() -> None:
    executor = StubExecutor(stdout="<html>502</html>")
    task = ContainerResourceCheckTask(
        container="nanofaas-word-stats-r1", resources=SPEC, executor=executor, role="host"
    )

    with pytest.raises(RuntimeError, match="was not JSON"):
        task.run(TaskInputs.empty())
