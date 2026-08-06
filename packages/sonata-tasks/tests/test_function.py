from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import Resource, TaskInputs
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.function import function_resource


@dataclass
class ScriptedExecutor:
    """Records specs and replays a result per matched argv fragment."""

    responses: dict[str, TaskResult] = field(default_factory=dict)
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        joined = " ".join(task.argv)
        for fragment, result in self.responses.items():
            if fragment in joined:
                return result
        return TaskResult(task_id="", status="passed", return_code=0)

    @property
    def titles(self) -> list[str]:
        return [spec.summary for spec in self.seen]


def _task(title: str, executor: ScriptedExecutor, *argv: str) -> CommandTask:
    return CommandTask(title=title, argv=argv or (title,), executor=executor)


def _resource(executor: ScriptedExecutor, **kwargs: object) -> Resource[None]:
    return function_resource(
        name="word-stats",
        register=_task("Register word-stats", executor, "register"),
        delete=_task("Delete word-stats", executor, "delete"),
        **kwargs,  # type: ignore[arg-type]
    )


def test_it_names_the_pair_after_the_function() -> None:
    resource = _resource(ScriptedExecutor())

    assert resource.title == "Acquire word-stats"
    assert resource.release_title == "Release word-stats"


def test_acquire_registers_and_release_deletes() -> None:
    executor = ScriptedExecutor()
    resource = _resource(executor)

    resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), None)

    assert executor.titles == ["Register word-stats", "Delete word-stats"]


def test_readiness_tasks_run_after_the_register_in_order() -> None:
    executor = ScriptedExecutor()
    resource = _resource(
        executor,
        readiness=(
            _task("Wait for word-stats", executor, "wait"),
            _task("Roll out word-stats", executor, "rollout"),
        ),
    )

    resource.acquire(TaskInputs.empty())

    assert executor.titles == [
        "Register word-stats",
        "Wait for word-stats",
        "Roll out word-stats",
    ]


def test_a_failed_register_deletes_best_effort_before_propagating() -> None:
    """The engine never releases an acquire that did not pass, so a register that
    created the function before failing would leak it."""
    executor = ScriptedExecutor(
        responses={"register": TaskResult(task_id="", status="failed", return_code=1, stderr="boom")}
    )
    resource = _resource(executor)

    with pytest.raises(RuntimeError, match="boom"):
        resource.acquire(TaskInputs.empty())

    assert executor.titles == ["Register word-stats", "Delete word-stats"]


def test_a_failed_readiness_also_deletes_because_the_register_did_land() -> None:
    executor = ScriptedExecutor(
        responses={"wait": TaskResult(task_id="", status="failed", return_code=1, stderr="timeout")}
    )
    resource = _resource(executor, readiness=(_task("Wait for word-stats", executor, "wait"),))

    with pytest.raises(RuntimeError, match="timeout"):
        resource.acquire(TaskInputs.empty())

    assert executor.titles[-1] == "Delete word-stats"


def test_a_failed_compensation_is_noted_without_masking_the_original_error() -> None:
    executor = ScriptedExecutor(
        responses={
            "register": TaskResult(task_id="", status="failed", return_code=1, stderr="conflict"),
            "delete": TaskResult(
                task_id="", status="failed", return_code=1, stderr="cleanup unavailable"
            ),
        }
    )
    resource = _resource(executor)

    with pytest.raises(RuntimeError, match="conflict") as captured:
        resource.acquire(TaskInputs.empty())

    notes = getattr(captured.value, "__notes__", [])
    assert len(notes) == 1
    assert "cleanup unavailable" in notes[0]


def test_requires_is_carried_onto_the_resource() -> None:
    helm = Resource(
        title="Acquire helm release",
        acquire=lambda _inputs: None,
        release=lambda _inputs, _value: None,
    )
    resource = _resource(ScriptedExecutor(), requires=(helm,))

    assert [dependency.title for dependency in resource.requires] == ["Acquire helm release"]


def test_a_function_always_releases_itself() -> None:
    """`keep` is for the VM and the platform on it, both expensive to rebuild.
    A registration costs a second to redo and, left behind, makes the next run
    fail with 409 — which it did, twice. Under opt-out retention a function that
    did not declare itself would be kept, so it declares itself."""
    assert _resource(ScriptedExecutor()).always_release is True
    assert _resource(ScriptedExecutor(), always_release=False).always_release is False
