from __future__ import annotations

from typing import Protocol, override

from sonata_engine import Task, TaskInputs, TaskOutcome

from sonata_tasks.loadtest import LoadtestOutcome, load_outcome


class ConservationEvaluator(Protocol):
    """What the conservation check needs from whoever assembled it.

    Kept as a protocol rather than the concrete legacy class so this package
    does not care where the k6 summary and the two metrics endpoints came
    from — reading a file and fetching two URLs is the caller's business.
    """

    def run(self) -> object: ...


class EvaluateConservationTask(Task[LoadtestOutcome]):
    """Reconcile what k6 sent with what the two control planes recorded.

    The traffic has to add up across the hop: k6's requests against the edge's
    successes, k6's offloaded count against the edge's offload counter and the
    cloud's successes, and the control function against neither. The evaluator
    already collects every divergence instead of stopping at the first, and
    names both sides with their numbers — so this adapter adds only the thing it
    was missing, which is a place in the graph.

    It reads the upstream outcome purely to refuse a run that did not happen: a
    conservation report over a load test that never ran would reconcile zero
    against zero and pass.
    """

    def __init__(
        self,
        *,
        evaluate: ConservationEvaluator,
        title: str = "Evaluate offload conservation",
    ) -> None:
        self.title = title
        self._evaluate = evaluate

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[LoadtestOutcome]:
        outcome = load_outcome(inputs, self.title)
        _ = self._evaluate.run()
        return TaskOutcome(value=outcome)
