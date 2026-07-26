from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import Selection
from workflow_tasks.execution.bindings import RoleBindings
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.cli import build_cli_plan

SUCCESS = '{"status":"success","output":{"words":2}}'


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=SUCCESS)


def _scenario(**overrides: object) -> ScenarioConfig:
    payload: dict[str, object] = {
        "workflow": "cli",
        "backend": "k8s",
        "functions": ["word-stats-java"],
    }
    payload.update(overrides)
    return ScenarioConfig.model_validate(payload)


def test_cli_plan_uses_the_selected_role_binding() -> None:
    host = RecordingExecutor()
    stack = RecordingExecutor()

    build_cli_plan(
        _scenario(),
        RoleBindings(host=host, stack=stack),
        cli_role="stack",
    ).run()

    assert host.seen == []
    assert [spec.summary for spec in stack.seen] == [
        "Build nanofaas-cli",
        "Apply word-stats-java",
        "List functions",
        "Invoke word-stats-java",
        "Delete word-stats-java",
    ]


def test_cli_plan_compiles_every_selected_function() -> None:
    plan = build_cli_plan(
        _scenario(
            functions=["word-stats-java", "json-transform-python"],
            resources={"word-stats-java": {"limits": {"memoryMiB": 512}}},
        ),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        endpoint="http://stack.example:8080",
    )

    assert [task.task_id for task in plan.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.acquire-word-stats-java",
        "003.acquire-json-transform-python",
        "004.list-functions",
        "005.invoke-word-stats-java",
        "006.release-word-stats-java",
        "007.invoke-json-transform-python",
        "008.release-json-transform-python",
    ]


def test_cli_plan_passes_the_endpoint_and_resolved_resources_through() -> None:
    executor = RecordingExecutor()

    build_cli_plan(
        _scenario(resources={"word-stats-java": {"limits": {"memoryMiB": 512}}}),
        RoleBindings(host=executor, stack=RecordingExecutor()),
        endpoint="http://stack.example:8080",
    ).run()

    apply_script = next(
        spec.argv[-1] for spec in executor.seen if spec.summary.startswith("Apply")
    )
    assert "http://stack.example:8080" in apply_script
    assert '"memoryMiB":512' in apply_script


def test_cli_plan_sends_only_the_payload_input_to_invoke() -> None:
    executor = RecordingExecutor()

    build_cli_plan(
        _scenario(),
        RoleBindings(host=executor, stack=RecordingExecutor()),
    ).run()

    invoke = next(spec for spec in executor.seen if spec.summary.startswith("Invoke"))
    assert '"input"' not in " ".join(invoke.argv)


def test_cli_plan_supports_slicing_by_slug() -> None:
    executor = RecordingExecutor()

    build_cli_plan(
        _scenario(),
        RoleBindings(host=executor, stack=RecordingExecutor()),
    ).run(select=Selection(only="list-functions"))

    assert [spec.summary for spec in executor.seen] == [
        "Apply word-stats-java",
        "List functions",
        "Delete word-stats-java",
    ]


def test_cli_plan_rejects_a_non_cli_scenario() -> None:
    with pytest.raises(ValueError, match="cli scenario"):
        build_cli_plan(
            ScenarioConfig.model_validate(
                {"workflow": "validate", "backend": "k8s", "functions": ["word-stats-java"]}
            ),
            RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        )
