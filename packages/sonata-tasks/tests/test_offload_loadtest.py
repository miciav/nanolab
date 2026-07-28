from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sonata_engine import TaskInputs
from sonata_engine.errors import NoUpstreamValueError
from workflow_tasks.loadtest.models import K6RunResult

from sonata_tasks.loadtest import LoadtestOutcome
from sonata_tasks.offload_loadtest import EvaluateConservationTask


def _outcome() -> LoadtestOutcome:
    return LoadtestOutcome(
        k6=K6RunResult(
            summary_path=Path("k6-summary.json"),
            started_at=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
            ended_at=datetime(2026, 7, 28, 10, 5, tzinfo=UTC),
            passed=True,
        )
    )


def _inputs(upstream: object) -> TaskInputs:
    return replace(TaskInputs.empty(), _upstream=upstream)


@dataclass
class FakeEvaluator:
    error: Exception | None = None
    calls: list[str] = field(default_factory=list[str])

    def run(self) -> object:
        self.calls.append("run")
        if self.error is not None:
            raise self.error
        return {"passed": True}


def test_it_evaluates_once_the_load_has_run() -> None:
    evaluator = FakeEvaluator()

    outcome = EvaluateConservationTask(evaluate=evaluator).run(_inputs(_outcome())).value

    assert evaluator.calls == ["run"]
    assert outcome is not None and outcome.k6.passed


def test_a_divergence_fails_the_run_carrying_every_failure() -> None:
    """The evaluator collects them all rather than stopping at the first, which
    is the difference between "the numbers disagree" and knowing which ones."""
    evaluator = FakeEvaluator(
        error=RuntimeError(
            "offload conservation check failed: "
            "k6 requests for offloadable (100.0) diverges from "
            "edge function_success_total for offloadable (80.0) beyond tolerance 5; "
            "control function was offloaded"
        )
    )

    with pytest.raises(RuntimeError, match="diverges from.*beyond tolerance 5"):
        EvaluateConservationTask(evaluate=evaluator).run(_inputs(_outcome()))


def test_it_refuses_to_reconcile_a_run_that_did_not_happen() -> None:
    """Zero against zero reconciles perfectly and proves nothing."""
    evaluator = FakeEvaluator()

    with pytest.raises(NoUpstreamValueError):
        EvaluateConservationTask(evaluate=evaluator).run(TaskInputs.empty())

    assert evaluator.calls == []


def test_it_refuses_an_upstream_that_is_not_a_load_run() -> None:
    with pytest.raises(RuntimeError, match="expected the load run's outcome"):
        EvaluateConservationTask(evaluate=FakeEvaluator()).run(_inputs("something else"))
