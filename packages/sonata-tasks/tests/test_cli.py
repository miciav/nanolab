from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest
from sonata_engine import Resource, Selection
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.cli import CliFunction, CliWorkflowRequest, build_cli_workflow
from sonata_tasks.command import CommandTask

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
    # The note names the resource it was compensating, and the nested error still
    # names the command that failed to clean up and why.
    assert "compensation for Acquire word-stats-java" in notes[0]
    assert "Delete word-stats-java failed" in notes[0]
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


def test_keep_still_deletes_the_function() -> None:
    """`--keep` is for the VM and the platform on it, both expensive to rebuild.
    A registration costs a second to redo and, left behind, makes the next run
    fail with 409 — which it did, twice, before this changed."""
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )
    workflow.keep = True

    workflow.run()

    assert executor.titles == [
        "Build nanofaas-cli",
        "Apply word-stats-java",
        "List functions",
        "Invoke word-stats-java",
        "Delete word-stats-java",
    ]


def test_the_workflow_can_run_entirely_on_the_stack_role() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,), cli_role="stack", build_role="stack"),
        _bindings(executor),
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


def test_an_external_resource_wraps_the_whole_workflow() -> None:
    executor = ScriptedExecutor()
    events: list[str] = []
    control_plane = Resource(
        title="Acquire local control plane",
        acquire=lambda _inputs: events.append("start"),
        release=lambda _inputs, _value: events.append("stop"),
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)),
        _bindings(executor),
        requires=(control_plane,),
    )

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.acquire-local-control-plane",
        "003.acquire-word-stats-java",
        "004.list-functions",
        "005.invoke-word-stats-java",
        "006.release-word-stats-java",
        "007.release-local-control-plane",
    ]

    workflow.run()
    assert events == ["start", "stop"]


def test_build_only_selection_does_not_acquire_the_external_resource() -> None:
    events: list[str] = []
    control_plane = Resource(
        title="Acquire local control plane",
        acquire=lambda _inputs: events.append("start"),
        release=lambda _inputs, _value: events.append("stop"),
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)),
        _bindings(ScriptedExecutor()),
        requires=(control_plane,),
    )

    workflow.run(select=Selection(only="build-nanofaas-cli"))

    assert events == []


def test_the_external_resource_is_released_when_a_task_fails() -> None:
    executor = ScriptedExecutor(
        responses={
            "invoke word-stats-java": TaskResult(
                task_id="", status="failed", return_code=1, stderr="boom"
            )
        }
    )
    events: list[str] = []
    control_plane = Resource(
        title="Acquire local control plane",
        acquire=lambda _inputs: events.append("start"),
        release=lambda _inputs, _value: events.append("stop"),
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)),
        _bindings(executor),
        requires=(control_plane,),
    )

    with pytest.raises(RuntimeError, match="boom"):
        workflow.run()

    assert events == ["start", "stop"]


def test_a_function_with_dockerfile_build_gets_artifact_and_image_tasks() -> None:
    executor = ScriptedExecutor()
    function = replace(
        FUNCTION,
        build_argv=("./gradlew", ":functions:java:word-stats:bootJar"),
        image_build_argv=(
            "docker",
            "build",
            "-t",
            FUNCTION.image,
            "-f",
            "functions/java/word-stats/Dockerfile",
            "functions/java/word-stats",
        ),
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(function,)), _bindings(executor)
    )

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.build-application-artifact-word-stats-java",
        "003.build-image-word-stats-java",
        "004.acquire-word-stats-java",
        "005.list-functions",
        "006.invoke-word-stats-java",
        "007.release-word-stats-java",
    ]


def test_the_control_plane_build_runs_before_images_and_acquire() -> None:
    control_plane = Resource(
        title="Acquire local control plane",
        acquire=lambda _inputs: None,
        release=lambda _inputs, _value: None,
    )
    function = replace(
        FUNCTION,
        build_argv=("./gradlew", ":functions:java:word-stats:bootBuildImage"),
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(function,)),
        _bindings(ScriptedExecutor()),
        control_plane_build_argv=("./gradlew", ":control-plane:bootJar"),
        requires=(control_plane,),
    )

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.build-local-control-plane",
        "003.build-image-word-stats-java",
        "004.acquire-local-control-plane",
        "005.acquire-word-stats-java",
        "006.list-functions",
        "007.invoke-word-stats-java",
        "008.release-word-stats-java",
        "009.release-local-control-plane",
    ]


def test_the_image_build_runs_before_the_function_is_registered() -> None:
    executor = ScriptedExecutor()
    function = replace(FUNCTION, build_argv=("./gradlew", ":functions:java:word-stats:bootBuildImage"))
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(function,)), _bindings(executor)
    )

    workflow.run()

    titles = executor.titles
    assert titles.index("Build image word-stats-java") < titles.index("Apply word-stats-java")


def test_requested_function_images_are_pushed_before_registration() -> None:
    function = replace(FUNCTION, build_argv=("docker", "build", "-t", FUNCTION.image, "."))

    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(function,), push_function_images=True), _bindings(ScriptedExecutor())
    )

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.build-image-word-stats-java",
        "003.push-image-localhost-5000-nanofaas-java-word-stats-e2e",
        "004.acquire-word-stats-java",
        "005.list-functions",
        "006.invoke-word-stats-java",
        "007.release-word-stats-java",
    ]


def test_without_build_argv_nothing_extra_is_emitted() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    assert "002.build-image-word-stats-java" not in [
        task.task_id for task in workflow.compile().tasks
    ]


def test_build_role_is_independent_of_cli_role() -> None:
    host = ScriptedExecutor()
    stack = ScriptedExecutor()
    function = replace(FUNCTION, build_argv=("./gradlew", ":functions:java:word-stats:bootBuildImage"))
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(function,), cli_role="stack", build_role="host"),
        RoleBindings(host=host, stack=stack),
        control_plane_build_argv=("./gradlew", ":control-plane:bootJar"),
    )

    workflow.run()

    assert [spec.summary for spec in host.seen] == [
        "Build nanofaas-cli",
        "Build local control plane",
        "Build image word-stats-java",
    ]
    assert [spec.summary for spec in stack.seen] == [
        "Apply word-stats-java",
        "List functions",
        "Invoke word-stats-java",
        "Delete word-stats-java",
    ]


@pytest.mark.parametrize("role", ["loadgen", "cloud", "arm-builder"])
def test_every_non_host_or_stack_build_role_is_rejected(role: ExecutionRole) -> None:
    with pytest.raises(ValueError, match="host or stack"):
        CliWorkflowRequest(functions=(FUNCTION,), build_role=role)


def test_bootstrap_tasks_run_between_build_and_the_first_resource() -> None:
    executor = ScriptedExecutor()
    events: list[str] = []
    vm = Resource(
        title="Acquire stack VM",
        acquire=lambda _inputs: events.append("acquire-vm") or "vm-info",
        release=lambda _inputs, _value: events.append("release-vm"),
            )
    bootstrap = (
        CommandTask(
            title="Provision base VM dependencies",
            argv=lambda inputs: ("ansible-playbook", inputs.resource(vm)),
            executor=executor,
            role="host",
        ),
        CommandTask(
            title="Sync repository into VM",
            argv=lambda inputs: ("rsync", inputs.resource(vm)),
            executor=executor,
            role="host",
        ),
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,), cli_role="stack"),
        _bindings(executor),
        requires=(vm,),
        bootstrap=bootstrap,
        bootstrap_requires=(vm,),
    )

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.acquire-stack-vm",
        "003.provision-base-vm-dependencies",
        "004.sync-repository-into-vm",
        "005.acquire-word-stats-java",
        "006.list-functions",
        "007.invoke-word-stats-java",
        "008.release-word-stats-java",
        "009.release-stack-vm",
    ]

    workflow.run()

    assert executor.titles == [
        "Build nanofaas-cli",
        "Provision base VM dependencies",
        "Sync repository into VM",
        "Apply word-stats-java",
        "List functions",
        "Invoke word-stats-java",
        "Delete word-stats-java",
    ]
    assert events == ["acquire-vm", "release-vm"]
    bootstrap_specs = [
        spec
        for spec in executor.seen
        if spec.summary in ("Provision base VM dependencies", "Sync repository into VM")
    ]
    assert all(spec.argv[-1] == "vm-info" for spec in bootstrap_specs)


def test_readiness_runs_after_apply_and_before_the_function_is_usable() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,), cli_role="stack"),
        _bindings(executor),
        readiness_timeout_seconds=60,
    )

    workflow.run()

    assert executor.titles == [
        "Build nanofaas-cli",
        "Apply word-stats-java",
        "Wait for deployment/fn-word-stats-java",
        "Roll out deployment/fn-word-stats-java",
        "List functions",
        "Invoke word-stats-java",
        "Delete word-stats-java",
    ]
    rollout = next(
        spec for spec in executor.seen if spec.summary.startswith("Roll out")
    )
    assert rollout.argv == (
        "kubectl",
        "-n",
        "nanofaas-e2e",
        "rollout",
        "status",
        "deployment/fn-word-stats-java",
        "--timeout=60s",
    )


def test_a_failed_readiness_wait_deletes_the_function_and_reraises() -> None:
    executor = ScriptedExecutor(
        responses={
            "rollout status": TaskResult(
                task_id="", status="failed", return_code=1, stderr="rollout timed out"
            )
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)),
        _bindings(executor),
        readiness_timeout_seconds=60,
    )

    with pytest.raises(RuntimeError, match="rollout timed out"):
        workflow.run()

    assert "Delete word-stats-java" in executor.titles
    assert "List functions" not in executor.titles


def test_function_requires_is_a_real_compiled_edge() -> None:
    events: list[str] = []
    helm = Resource(
        title="Acquire Helm release",
        acquire=lambda _inputs: events.append("helm-acquire"),
        release=lambda _inputs, _value: events.append("helm-release"),
            )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)),
        _bindings(ScriptedExecutor()),
        function_requires=(helm,),
    )

    compiled = workflow.compile()
    function_acquire = next(
        task for task in compiled.tasks if task.task_id == "003.acquire-word-stats-java"
    )
    assert function_acquire.resource is not None
    assert function_acquire.resource.requires == (helm,)
    assert [task.task_id for task in compiled.tasks] == [
        "001.build-nanofaas-cli",
        "002.acquire-helm-release",
        "003.acquire-word-stats-java",
        "004.list-functions",
        "005.invoke-word-stats-java",
        "006.release-word-stats-java",
        "007.release-helm-release",
    ]

    workflow.run()

    assert events == ["helm-acquire", "helm-release"]


def test_slicing_the_invoke_task_keeps_function_requires_transitively() -> None:
    events: list[str] = []
    helm = Resource(
        title="Acquire Helm release",
        acquire=lambda _inputs: events.append("helm-acquire"),
        release=lambda _inputs, _value: events.append("helm-release"),
            )
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)),
        _bindings(executor),
        function_requires=(helm,),
    )

    workflow.run(select=Selection(only="invoke-word-stats-java"))

    assert events == ["helm-acquire", "helm-release"]
    assert executor.titles == [
        "Apply word-stats-java",
        "Invoke word-stats-java",
        "Delete word-stats-java",
    ]


def test_without_readiness_timeout_no_extra_commands_are_emitted() -> None:
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
