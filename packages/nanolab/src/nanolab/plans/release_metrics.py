"""Pure computation Sonata tasks wrapping nanolab.release.metrics.

Neither task needs an executor or produces shell commands — they are pure
Python wrappers that can run in any workflow role.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import override

from sonata_engine import Task, TaskInputs, TaskOutcome
from nanolab.release.metrics import (
    PerformanceAggregate,
    PerformanceProfile,
    RegressionDecision,
    RegressionPolicy,
    aggregate_runs,
    evaluate_regression,
)


class AggregateBenchmarks(Task[PerformanceAggregate]):
    """Read benchmark summaries from disk and call aggregate_runs()."""

    def __init__(
        self,
        *,
        run_dir: Path,
        benchmark_count: int,
        profile: PerformanceProfile,
        title: str = "Aggregate benchmarks",
    ) -> None:
        self.title = title
        self._run_dir = run_dir
        self._benchmark_count = benchmark_count
        self._profile = profile

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[PerformanceAggregate]:
        del inputs
        summaries = []
        for i in range(1, self._benchmark_count + 1):
            path = self._run_dir / f"run-{i}" / "summary.json"
            if not path.is_file():
                raise RuntimeError(f"benchmark summary not found: {path}")
            summaries.append(json.loads(path.read_text(encoding="utf-8")))
        return TaskOutcome(value=aggregate_runs(self._profile, tuple(summaries)))


class EvaluateRegressionGate(Task[RegressionDecision]):
    """Evaluate release gates by calling evaluate_regression()."""

    def __init__(
        self,
        *,
        aggregate: PerformanceAggregate,
        baseline: PerformanceAggregate | None,
        policy: RegressionPolicy,
        k6_passed: bool,
        autoscaling_passed: bool,
        title: str = "Evaluate regression gate",
    ) -> None:
        self.title = title
        self._aggregate = aggregate
        self._baseline = baseline
        self._policy = policy
        self._k6_passed = k6_passed
        self._autoscaling_passed = autoscaling_passed

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[RegressionDecision]:
        del inputs
        return TaskOutcome(
            value=evaluate_regression(
                self._aggregate,
                self._baseline,
                self._policy,
                k6_passed=self._k6_passed,
                autoscaling_passed=self._autoscaling_passed,
            )
        )
