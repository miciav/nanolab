from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest
from sonata_engine import Selection
from workflow_tasks.execution.bindings import RoleBindings
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.cli import CliFunction, CliWorkflowRequest, build_cli_workflow

SUCCESS = '{"status":"success","output":{"words":2}}'


@dataclass
class ScriptedExecutor:
    """Records specs and replays a result per matched argv fragment.

    `responses` maps a substring of the joined argv to the result to return;
    anything unmatched passes with empty output.
    """

    responses: dict[str, TaskResult] = field(default_factory=dict)
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        joined = " ".join(task.argv)
        for fragment, result in self.responses.items():
            if fragment in joined:
                return result
        return TaskResult(task_id="", status="passed", return_code=0, stdout=SUCCESS)

    @property
    def titles(self) -> list[str]:
        return [spec.summary for spec in self.seen]


FUNCTION = CliFunction(
    name="word-stats-java",
    image="localhost:5000/nanofaas/java-word-stats:e2e",
    payload='{"text":"hello world"}',
)
OTHER = CliFunction(
    name="json-transform-python",
    image="localhost:5000/nanofaas/python-json:e2e",
    payload='{"a":1}',
)


def _bindings(executor: ScriptedExecutor) -> RoleBindings:
    return RoleBindings(host=executor, stack=executor)


def test_a_single_function_compiles_to_the_expected_topology() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    compiled = workflow.compile()

    assert [task.task_id for task in compiled.tasks] == [
        "001.build-nanofaas-cli",
        "002.acquire-word-stats-java",
        "003.list-functions",
        "004.invoke-word-stats-java",
        "005.release-word-stats-java",
    ]


def test_running_it_applies_before_listing_and_deletes_last() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    workflow.run()

    assert executor.titles == [
        "Build nanofaas-cli",
        "Apply word-stats-java",
        "List functions",
        "Invoke word-stats-java",
        "Delete word-stats-java",
    ]


def test_the_function_is_deleted_even_when_invoke_fails() -> None:
    executor = ScriptedExecutor(
        responses={
            "invoke word-stats-java": TaskResult(
                task_id="", status="failed", return_code=1, stderr="unreachable"
            )
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    with pytest.raises(RuntimeError, match="unreachable"):
        workflow.run()

    assert "Delete word-stats-java" in executor.titles


def test_a_failed_apply_compensates_best_effort_before_propagating() -> None:
    # The apply command is a bash script whose CLI arguments are shell-quoted,
    # so "fn apply" does not appear literally in the joined argv. "mktemp" does,
    # and only there.
    executor = ScriptedExecutor(
        responses={
            "mktemp": TaskResult(
                task_id="", status="failed", return_code=1, stderr="conflict"
            )
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    with pytest.raises(RuntimeError, match="conflict"):
        workflow.run()

    # The apply may have registered the function before failing, so the task
    # compensates itself. The engine never releases an acquire that did not pass.
    assert executor.titles == [
        "Build nanofaas-cli",
        "Apply word-stats-java",
        "Delete word-stats-java",
    ]


def test_a_failed_compensation_is_noted_without_masking_the_apply_error() -> None:
    executor = ScriptedExecutor(
        responses={
            "mktemp": TaskResult(
                task_id="", status="failed", return_code=1, stderr="apply conflict"
            ),
            "fn delete word-stats-java": TaskResult(
                task_id="", status="failed", return_code=1, stderr="cleanup unavailable"
            ),
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    with pytest.raises(RuntimeError, match="apply conflict") as captured:
        workflow.run()

    notes = getattr(captured.value, "__notes__", [])
    assert len(notes) == 1
    assert "Best-effort delete after a failed apply failed" in notes[0]
    assert "cleanup unavailable" in notes[0]


def test_invoke_rejects_a_non_success_status() -> None:
    executor = ScriptedExecutor(
        responses={
            "invoke word-stats-java": TaskResult(
                task_id="",
                status="passed",
                return_code=0,
                stdout='{"status":"error","output":{}}',
            )
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    with pytest.raises(RuntimeError, match="did not report success"):
        workflow.run()


def test_invoke_rejects_a_response_without_output() -> None:
    executor = ScriptedExecutor(
        responses={
            "invoke word-stats-java": TaskResult(
                task_id="", status="passed", return_code=0, stdout='{"status":"success"}'
            )
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    with pytest.raises(RuntimeError, match="carried no output"):
        workflow.run()


def test_invoke_rejects_malformed_json() -> None:
    executor = ScriptedExecutor(
        responses={
            "invoke word-stats-java": TaskResult(
                task_id="", status="passed", return_code=0, stdout="<html>502</html>"
            )
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    with pytest.raises(RuntimeError, match="was not JSON"):
        workflow.run()


def test_two_functions_release_each_one_after_its_last_consumer() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION, OTHER)), _bindings(executor)
    )

    compiled = workflow.compile()

    assert [task.task_id for task in compiled.tasks] == [
        "001.build-nanofaas-cli",
        "002.acquire-word-stats-java",
        "003.acquire-json-transform-python",
        "004.list-functions",
        "005.invoke-word-stats-java",
        "006.release-word-stats-java",
        "007.invoke-json-transform-python",
        "008.release-json-transform-python",
    ]


def test_selecting_one_invoke_keeps_that_function_lifecycle_only() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION, OTHER)), _bindings(executor)
    )

    workflow.run(select=Selection(only="invoke-json-transform-python"))

    assert executor.titles == [
        "Apply json-transform-python",
        "Invoke json-transform-python",
        "Delete json-transform-python",
    ]


def test_the_endpoint_and_namespace_reach_every_cli_call() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(
            functions=(FUNCTION,),
            endpoint="http://stack.example:8080",
            namespace="research",
        ),
        _bindings(executor),
    )

    workflow.run()

    cli_calls = [spec for spec in executor.seen if spec.summary != "Build nanofaas-cli"]
    assert all("http://stack.example:8080" in " ".join(spec.argv) for spec in cli_calls)
    assert all("research" in " ".join(spec.argv) for spec in cli_calls)


def test_apply_builds_the_manifest_on_the_target_not_in_this_process() -> None:
    executor = ScriptedExecutor()
    function = replace(
        FUNCTION,
        image="registry.example/research:v2",
        resources={"limits": {"cpu": 1.0, "memoryMiB": 512}},
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(function,)), _bindings(executor)
    )

    workflow.run()

    apply_spec = next(spec for spec in executor.seen if spec.summary.startswith("Apply"))
    script = apply_spec.argv[-1]
    assert apply_spec.argv[:2] == ("bash", "-lc")
    assert "mktemp" in script
    assert "registry.example/research:v2" in script
    assert '"memoryMiB":512' in script


def test_delete_keeps_real_cli_failures_visible() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    workflow.run()

    delete_spec = next(spec for spec in executor.seen if spec.summary.startswith("Delete"))
    # The nanoFaaS CLI already exits 0 when DELETE returns 404. Accepting 1
    # would also hide network, server, and other real cleanup failures.
    assert delete_spec.expected_exit_codes == frozenset({0})


def test_the_workflow_can_run_entirely_on_the_stack_role() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,), cli_role="stack"), _bindings(executor)
    )

    workflow.run()

    assert all(spec.execution_role == "stack" for spec in executor.seen)


@pytest.mark.parametrize("role", ["loadgen", "cloud", "arm-builder"])
def test_every_non_host_or_stack_role_is_rejected(role: ExecutionRole) -> None:
    with pytest.raises(ValueError, match="host or stack"):
        CliWorkflowRequest(functions=(FUNCTION,), cli_role=role)


def test_at_least_one_function_is_required() -> None:
    with pytest.raises(ValueError, match="at least one function"):
        CliWorkflowRequest(functions=())
