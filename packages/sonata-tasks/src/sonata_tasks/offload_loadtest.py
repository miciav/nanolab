from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, override

from sonata_engine import Steps, Task, TaskInputs, TaskOutcome, Workflow
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor

from sonata_tasks.loadtest import LoadtestOutcome, load_outcome
from sonata_tasks.platform import PlatformRequest, add_platform


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


@dataclass(frozen=True, slots=True)
class OffloadLoadtestRequest:
    """The two sides of the hop, each its own platform.

    Two clusters, so two `PlatformRequest`s rather than one with a flag: they
    differ in role, in Helm values — the edge's chart has to know where to
    offload to — and in which functions they register.
    """

    cloud: PlatformRequest
    edge: PlatformRequest

    def __post_init__(self) -> None:
        if self.cloud.role == self.edge.role:
            raise ValueError(
                f"cloud and edge must run on different roles; both are {self.cloud.role!r}"
            )


def build_offload_loadtest_workflow(
    request: OffloadLoadtestRequest,
    bindings: RoleBindings,
    *,
    load: Steps,
    workflow_id: str = "offload-loadtest",
    cwd: Path | None = None,
) -> Workflow:
    """Two platforms, functions registered across them, then load over the hop.

    The cloud comes up first and is acquired first, so the edge's chart can be
    installed already pointing at something that answers. Both halves are
    `add_platform`, the same code `validate` and `loadtest` use — the only
    reason this workflow needed anything new is the conservation check.

    The load declares every function resource on both sides, so the compiler
    releases them after it: a load test that fails still deregisters what it
    registered, on both clusters.
    """
    executor = RoleBoundCommandTaskExecutor(bindings)
    workflow = Workflow(workflow_id=workflow_id)
    cloud = add_platform(workflow, request.cloud, executor=executor, cwd=cwd)
    edge = add_platform(workflow, request.edge, executor=executor, cwd=cwd)
    workflow.add(
        load,
        requires=(*cloud.resources, *cloud.functions, *edge.resources, *edge.functions),
    )
    return workflow
