from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask

_SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{([^}]*)\})?\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def _metric_sum(scrape: str, name: str, labels: Mapping[str, str]) -> tuple[int, float]:
    matches = 0
    total = 0.0
    for line in scrape.splitlines():
        match = _SAMPLE.match(line)
        if match is None or match.group(1) != name:
            continue
        actual = {key: value.replace(r'\"', '"').replace(r"\\", "\\") for key, value in _LABEL.findall(match.group(2) or "")}
        if all(actual.get(key) == value for key, value in labels.items()):
            matches += 1
            total += float(match.group(3))
    return matches, total


class PrometheusScrapeCheckTask(CommandTask):
    """Scrape an actuator endpoint and assert which samples are there.

    `expect` names samples that must appear, `reject` samples that must not. Both
    are matched as plain substrings of the scrape, which is how the shell version
    did it with `grep -F` — the difference is what happens when one does not hold.

    That version was two `grep -F` calls joined by `&&`: a failure exited 1 and
    said nothing, so a missing metric, one carrying the wrong label, and an
    unexpected failure counter were the same non-zero exit. This names the sample
    that broke the expectation, and which way.
    """

    def __init__(
        self,
        *,
        url: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        expect: tuple[str, ...] = (),
        reject: tuple[str, ...] = (),
        title: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        if not expect and not reject:
            raise ValueError("a scrape check must expect or reject at least one sample")

        def check(result: TaskResult) -> None:
            for sample in expect:
                if sample not in result.stdout:
                    raise RuntimeError(f"{url}: expected sample not scraped: {sample}")
            for sample in reject:
                if sample in result.stdout:
                    raise RuntimeError(f"{url}: sample present and should not be: {sample}")

        super().__init__(
            title=title or f"Check the metrics at {url}",
            argv=("curl", "-fsS", url),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=check,
        )


class PrometheusMinimumCheckTask(CommandTask):
    """Assert public Prometheus counters have accumulated enough samples."""

    def __init__(
        self,
        *,
        url: str,
        minimums: tuple[tuple[str, Mapping[str, str], float], ...],
        any_minimums: tuple[tuple[tuple[str, ...], Mapping[str, str], float], ...] = (),
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        title: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        if not minimums and not any_minimums:
            raise ValueError("a metric minimum check needs at least one expectation")

        def require(scrape: str, names: tuple[str, ...], labels: Mapping[str, str], minimum: float) -> None:
            for name in names:
                matches, total = _metric_sum(scrape, name, labels)
                if matches:
                    if total < minimum:
                        raise RuntimeError(f"{url}: {name}{dict(labels)} sum was {total}, expected >= {minimum}")
                    return
            raise RuntimeError(f"{url}: none of {names} appeared with labels {dict(labels)}")

        def check(result: TaskResult) -> None:
            for name, labels, minimum in minimums:
                require(result.stdout, (name,), labels, minimum)
            for names, labels, minimum in any_minimums:
                require(result.stdout, names, labels, minimum)

        super().__init__(
            title=title or f"Check metric values at {url}",
            argv=("curl", "-fsS", url),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=check,
        )
