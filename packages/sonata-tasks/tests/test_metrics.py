from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.metrics import PrometheusMinimumCheckTask
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult


@dataclass
class Executor:
    stdout: str
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=self.stdout)


def test_metric_check_sums_matching_samples_and_honors_labels() -> None:
    executor = Executor(
        'function_success_total{function="word-stats"} 1\n'
        'function_success_total{function="word-stats"} 2\n'
        'function_success_total{function="other"} 100\n'
    )

    _ = PrometheusMinimumCheckTask(
        url="http://cp:8081/actuator/prometheus",
        minimums=(("function_success_total", {"function": "word-stats"}, 3),),
        executor=executor,
        role="stack",
    ).run(TaskInputs.empty())


def test_metric_check_rejects_a_metric_below_the_requested_minimum() -> None:
    executor = Executor('function_cold_start_total{function="word-stats"} 0\n')
    task = PrometheusMinimumCheckTask(
        url="http://cp:8081/actuator/prometheus",
        minimums=(("function_cold_start_total", {"function": "word-stats"}, 1),),
        executor=executor,
        role="stack",
    )

    with pytest.raises(RuntimeError, match="function_cold_start_total"):
        _ = task.run(TaskInputs.empty())


def test_metric_check_accepts_the_first_available_alternative() -> None:
    executor = Executor('function_init_duration_ms_count{function="word-stats"} 1\n')

    _ = PrometheusMinimumCheckTask(
        url="http://cp:8081/actuator/prometheus",
        minimums=(),
        any_minimums=(
            (("function_init_duration_ms_seconds_count", "function_init_duration_ms_count"), {"function": "word-stats"}, 1),
        ),
        executor=executor,
        role="stack",
    ).run(TaskInputs.empty())
