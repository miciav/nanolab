"""Tests for sonata_tasks.release_metrics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest
from sonata_engine import TaskInputs
from nanolab.release.metrics import (
    PerformanceAggregate,
    PerformanceProfile,
    RegressionPolicy,
)

from nanolab.plans.release_metrics import AggregateBenchmarks, EvaluateRegressionGate


_PROFILE = PerformanceProfile(
    name="nanofaas-simple",
    provider="azure",
    stack_vm="Standard_D4s_v5",
    loadgen_vm="Standard_D4s_v5",
    architecture="amd64",
    flavor="simple",
    scenario="hello-world",
)

_POLICY = RegressionPolicy(
    throughput_max_loss_percent=10.0,
    p95_max_increase_percent=20.0,
    error_rate_max=0.01,
)

_BASELINE_AGGREGATE = PerformanceAggregate(
    profile=_PROFILE,
    run_count=3,
    metrics={
        "throughputRps": 500.0,
        "errorRate": 0.0,
        "latencyP50Ms": 10.0,
        "latencyP95Ms": 20.0,
        "latencyP99Ms": 50.0,
        "queueWaitSeconds": 0.0,
        "coldStarts": 0.0,
        "controlPlaneCpuPeak": 0.5,
        "controlPlaneHeapPeakBytes": 256_000_000.0,
        "peakReplicas": 3.0,
    },
)


def _summary(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A minimal summary dict suitable for aggregate_runs()."""
    base: dict[str, Any] = {
        "k6": {
            "http_reqs": {"values": {"rate": 500.0}},
            "http_req_failed": {"values": {"rate": 0.0}},
            "http_req_duration": {"values": {"p(50)": 10.0, "p(95)": 20.0, "p(99)": 50.0}},
        },
        "prometheus": {
            "function_queue_wait_count": {"delta": 10},
            "function_queue_wait_sum": {"delta": 0.5},
            "function_cold_start_total": {"delta": 0},
            "process_cpu_usage": {"max": 0.5},
            "jvm_heap_used_bytes": {"max": 256_000_000},
        },
        "autoscaling": {"max_replicas_observed": 3},
    }
    if overrides:
        _deep_merge(base, overrides)
    return base


def _deep_merge(base: dict[str, Any], overrides: Mapping[str, Any]) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and key in base and isinstance(base[key], dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


# ---------------------------------------------------------------------------
# AggregateBenchmarks
# ---------------------------------------------------------------------------


def test_aggregate_benchmarks_produces_performance_aggregate(tmp_path: Path) -> None:
    (tmp_path / "run-1").mkdir()
    (tmp_path / "run-1" / "summary.json").write_text(json.dumps(_summary()))
    task = AggregateBenchmarks(
        run_dir=tmp_path,
        benchmark_count=1,
        profile=_PROFILE,
    )
    outcome = task.run(TaskInputs.empty())
    assert isinstance(outcome.value, PerformanceAggregate)
    assert outcome.value.profile == _PROFILE
    assert outcome.value.run_count == 1
    assert outcome.value.metrics["throughputRps"] == 500.0


def test_aggregate_benchmarks_median_over_multiple_runs(tmp_path: Path) -> None:
    fast = _summary({"k6": {"http_reqs": {"values": {"rate": 100.0}}}})
    medium = _summary({"k6": {"http_reqs": {"values": {"rate": 200.0}}}})
    slow = _summary({"k6": {"http_reqs": {"values": {"rate": 300.0}}}})
    for i, s in enumerate((fast, medium, slow), 1):
        (tmp_path / f"run-{i}").mkdir()
        (tmp_path / f"run-{i}" / "summary.json").write_text(json.dumps(s))
    task = AggregateBenchmarks(
        run_dir=tmp_path,
        benchmark_count=3,
        profile=_PROFILE,
    )
    outcome = task.run(TaskInputs.empty())
    assert outcome.value is not None
    assert outcome.value.metrics["throughputRps"] == 200.0  # median
    assert outcome.value.run_count == 3


def test_aggregate_benchmarks_fails_on_missing_summary(tmp_path: Path) -> None:
    task = AggregateBenchmarks(
        run_dir=tmp_path,
        benchmark_count=1,
        profile=_PROFILE,
    )
    with pytest.raises(RuntimeError, match="benchmark summary not found"):
        task.run(TaskInputs.empty())


# ---------------------------------------------------------------------------
# EvaluateRegressionGate
# ---------------------------------------------------------------------------


def test_evaluate_regression_gate_passes_with_no_baseline() -> None:
    aggregate = PerformanceAggregate(
        profile=_PROFILE,
        run_count=3,
        metrics={
            "throughputRps": 500.0,
            "errorRate": 0.0,
            "latencyP50Ms": 10.0,
            "latencyP95Ms": 20.0,
            "latencyP99Ms": 50.0,
            "queueWaitSeconds": 0.0,
            "coldStarts": 0.0,
            "controlPlaneCpuPeak": 0.5,
            "controlPlaneHeapPeakBytes": 256_000_000.0,
            "peakReplicas": 3.0,
        },
    )
    task = EvaluateRegressionGate(
        aggregate=aggregate,
        baseline=None,
        policy=_POLICY,
        k6_passed=True,
        autoscaling_passed=True,
    )
    outcome = task.run(TaskInputs.empty())
    assert outcome.value is not None
    assert outcome.value.passed is True
    assert outcome.value.establishes_baseline is True
    assert outcome.value.failures == ()


def test_evaluate_regression_gate_fails_on_k6() -> None:
    aggregate = _make_healthy_aggregate()
    task = EvaluateRegressionGate(
        aggregate=aggregate,
        baseline=None,
        policy=_POLICY,
        k6_passed=False,
        autoscaling_passed=True,
    )
    outcome = task.run(TaskInputs.empty())
    assert outcome.value is not None
    assert outcome.value.passed is False
    assert "k6 gate failed" in outcome.value.failures


def test_evaluate_regression_gate_fails_on_autoscaling() -> None:
    aggregate = _make_healthy_aggregate()
    task = EvaluateRegressionGate(
        aggregate=aggregate,
        baseline=None,
        policy=_POLICY,
        k6_passed=True,
        autoscaling_passed=False,
    )
    outcome = task.run(TaskInputs.empty())
    assert outcome.value is not None
    assert outcome.value.passed is False
    assert "autoscaling gate failed" in outcome.value.failures


def test_evaluate_regression_gate_fails_on_throughput_loss() -> None:
    aggregate = PerformanceAggregate(
        profile=_PROFILE,
        run_count=3,
        metrics={
            "throughputRps": 400.0,
            "errorRate": 0.0,
            "latencyP50Ms": 10.0,
            "latencyP95Ms": 20.0,
            "latencyP99Ms": 50.0,
            "queueWaitSeconds": 0.0,
            "coldStarts": 0.0,
            "controlPlaneCpuPeak": 0.5,
            "controlPlaneHeapPeakBytes": 256_000_000.0,
            "peakReplicas": 3.0,
        },
    )
    # 500 -> 400 is 20% loss, exceeds 10% policy
    task = EvaluateRegressionGate(
        aggregate=aggregate,
        baseline=_BASELINE_AGGREGATE,
        policy=_POLICY,
        k6_passed=True,
        autoscaling_passed=True,
    )
    outcome = task.run(TaskInputs.empty())
    assert outcome.value is not None
    assert outcome.value.passed is False
    assert "throughput loss" in outcome.value.failures[0]


def test_evaluate_regression_gate_fails_on_p95_increase() -> None:
    aggregate = PerformanceAggregate(
        profile=_PROFILE,
        run_count=3,
        metrics={
            "throughputRps": 500.0,
            "errorRate": 0.0,
            "latencyP50Ms": 10.0,
            "latencyP95Ms": 30.0,
            "latencyP99Ms": 50.0,
            "queueWaitSeconds": 0.0,
            "coldStarts": 0.0,
            "controlPlaneCpuPeak": 0.5,
            "controlPlaneHeapPeakBytes": 256_000_000.0,
            "peakReplicas": 3.0,
        },
    )
    # 20 -> 30 is 50% increase, exceeds 20% policy
    task = EvaluateRegressionGate(
        aggregate=aggregate,
        baseline=_BASELINE_AGGREGATE,
        policy=_POLICY,
        k6_passed=True,
        autoscaling_passed=True,
    )
    outcome = task.run(TaskInputs.empty())
    assert outcome.value is not None
    assert outcome.value.passed is False
    assert "p95 increase" in outcome.value.failures[0]


def test_evaluate_regression_gate_fails_on_high_error_rate() -> None:
    aggregate = PerformanceAggregate(
        profile=_PROFILE,
        run_count=3,
        metrics={
            "throughputRps": 500.0,
            "errorRate": 0.05,
            "latencyP50Ms": 10.0,
            "latencyP95Ms": 20.0,
            "latencyP99Ms": 50.0,
            "queueWaitSeconds": 0.0,
            "coldStarts": 0.0,
            "controlPlaneCpuPeak": 0.5,
            "controlPlaneHeapPeakBytes": 256_000_000.0,
            "peakReplicas": 3.0,
        },
    )
    # 0.05 > 0.01 policy
    task = EvaluateRegressionGate(
        aggregate=aggregate,
        baseline=None,
        policy=_POLICY,
        k6_passed=True,
        autoscaling_passed=True,
    )
    outcome = task.run(TaskInputs.empty())
    assert outcome.value is not None
    assert outcome.value.passed is False
    assert "error rate" in outcome.value.failures[0]


def test_evaluate_regression_gate_does_not_establish_baseline_with_baseline() -> None:
    aggregate = _make_healthy_aggregate()
    task = EvaluateRegressionGate(
        aggregate=aggregate,
        baseline=_BASELINE_AGGREGATE,
        policy=_POLICY,
        k6_passed=True,
        autoscaling_passed=True,
    )
    outcome = task.run(TaskInputs.empty())
    assert outcome.value is not None
    assert outcome.value.passed is True
    assert outcome.value.establishes_baseline is False  # baseline already exists


def test_evaluate_regression_gate_passes_on_acceptable_regression() -> None:
    aggregate = PerformanceAggregate(
        profile=_PROFILE,
        run_count=3,
        metrics={
            "throughputRps": 480.0,
            "errorRate": 0.0,
            "latencyP50Ms": 10.0,
            "latencyP95Ms": 21.0,
            "latencyP99Ms": 50.0,
            "queueWaitSeconds": 0.0,
            "coldStarts": 0.0,
            "controlPlaneCpuPeak": 0.5,
            "controlPlaneHeapPeakBytes": 256_000_000.0,
            "peakReplicas": 3.0,
        },
    )
    # 500 -> 480 is 4% throughput loss (under 10%), 20 -> 21 is 5% p95 increase (under 20%)
    task = EvaluateRegressionGate(
        aggregate=aggregate,
        baseline=_BASELINE_AGGREGATE,
        policy=_POLICY,
        k6_passed=True,
        autoscaling_passed=True,
    )
    outcome = task.run(TaskInputs.empty())
    assert outcome.value is not None
    assert outcome.value.passed is True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_healthy_aggregate() -> PerformanceAggregate:
    return PerformanceAggregate(
        profile=_PROFILE,
        run_count=3,
        metrics={
            "throughputRps": 500.0,
            "errorRate": 0.0,
            "latencyP50Ms": 10.0,
            "latencyP95Ms": 20.0,
            "latencyP99Ms": 50.0,
            "queueWaitSeconds": 0.0,
            "coldStarts": 0.0,
            "controlPlaneCpuPeak": 0.5,
            "controlPlaneHeapPeakBytes": 256_000_000.0,
            "peakReplicas": 3.0,
        },
    )
