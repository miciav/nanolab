"""Pure computation Sonata tasks wrapping nanolab.release.metrics.

Neither task needs an executor or produces shell commands — they are pure
Python wrappers that can run in any workflow role.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence, override

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
    """Aggregate benchmark summaries by calling aggregate_runs()."""

    def __init__(
        self,
        *,
        benchmark_runs: Sequence[Mapping[str, Any]],
        profile: PerformanceProfile,
        title: str = "Aggregate benchmarks",
    ) -> None:
        self.title = title
        self._benchmark_runs = benchmark_runs
        self._profile = profile

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[PerformanceAggregate]:
        del inputs
        return TaskOutcome(value=aggregate_runs(self._profile, self._benchmark_runs))


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
