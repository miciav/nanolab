from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from sonata_engine import Resource, TaskInputs
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.metrics import (
    PrometheusMinimumCheckTask as SharedPrometheusMinimumCheckTask,
    PrometheusScrapeCheckTask as SharedPrometheusScrapeCheckTask,
    metric_sum,
)

Endpoint = str | Resource[str]


class PrometheusScrapeCheckTask(SharedPrometheusScrapeCheckTask):
    """Product endpoint resolver around Sonata's transport-only scrape check."""

    def __init__(self, *, url: Endpoint, executor: CommandTaskExecutor, role: str,
                 expect: tuple[str, ...] = (), reject: tuple[str, ...] = (),
                 title: str | None = None, cwd: Path | None = None) -> None:
        endpoint = url if isinstance(url, str) else lambda inputs: _actuator_url(url, inputs)
        key = None if isinstance(url, str) else f"nanolab-actuator-scrape:v1:{url.title}"
        super().__init__(url=endpoint, executor=executor, role=role, expect=expect,
                         reject=reject, title=title, options=CommandOptions(cwd=cwd),
                         semantic_key=key)


def _actuator_url(endpoint: Resource[str], inputs: TaskInputs) -> str:
    return inputs.resource(endpoint).replace(":8080", ":8081") + "/actuator/prometheus"


class PrometheusMinimumCheckTask(SharedPrometheusMinimumCheckTask):
    def __init__(self, *, url: Endpoint,
                 minimums: tuple[tuple[str, Mapping[str, str], float], ...],
                 any_minimums: tuple[tuple[tuple[str, ...], Mapping[str, str], float], ...] = (),
                 executor: CommandTaskExecutor, role: str, title: str | None = None,
                 cwd: Path | None = None) -> None:
        endpoint = url if isinstance(url, str) else lambda inputs: _actuator_url(url, inputs)
        key = None if isinstance(url, str) else f"nanolab-actuator:v1:{url.title}"
        super().__init__(url=endpoint, minimums=minimums, any_minimums=any_minimums,
                         executor=executor, role=role, title=title,
                         options=CommandOptions(cwd=cwd), semantic_key=key)


__all__ = ["PrometheusMinimumCheckTask", "PrometheusScrapeCheckTask", "metric_sum"]
