from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any

import pytest
from sonata_engine import Resource, TaskInputs, Workflow
from sonata_engine.workflow.context import bind_workflow_sink
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.validate import (
    ValidateFunction,
    ValidateWorkflowRequest,
    build_validate_workflow,
)

FUNCTION = ValidateFunction(
    name="word-stats-java",
    image="localhost:5000/nanofaas/java-word-stats:e2e",
    payload='{"text":"a b"}',
    build_argv=("./gradlew", ":functions:java:word-stats:bootBuildImage"),
    resources={"requests": {"cpu": 0.5, "memoryMiB": 256}, "limits": {"cpu": 1, "memoryMiB": 512}},
)

QUEUE_PROBE = ValidateFunction(
    name="k8s-sync-queue",
    image="localhost:5000/nanofaas/java-warm-echo:e2e",
    payload='{"input":{"message":"warmup"}}',
    build_argv=("./gradlew", ":services:java:warm-echo:bootJar", "--quiet"),
    image_build_argv=(
        "docker",
        "build",
        "-t",
        "localhost:5000/nanofaas/java-warm-echo:e2e",
        "-f",
        "services/java/warm-echo/Dockerfile",
        "services/java/warm-echo",
    ),
    concurrency=1,
)


# 0.5 CPU -> 512 shares, 1 CPU -> 1e9 nanocpus, 256/512 MiB, reservation set
# because request and limit differ.
CONTAINER_AT_DECLARED_LIMITS = (
    '{"CpuShares":512,"NanoCpus":1000000000,'
    '"MemoryReservation":268435456,"Memory":536870912}'
)
DEPLOYMENT_AT_DECLARED_LIMITS = (
    '{"spec":{"template":{"spec":{"containers":[{"resources":{'
    '"requests":{"cpu":"500m","memory":"256Mi"},'
    '"limits":{"cpu":"1","memory":"512Mi"}}}]}}}}'
)


@dataclass
class ScriptedExecutor:
    """Answers every command, with per-command overrides keyed by a substring."""

    responses: dict[str, TaskResult] = field(default_factory=dict[str, TaskResult])
    seen: list[CommandTaskSpec] = field(default_factory=list[CommandTaskSpec])
    default_stdout: str = '{"status":"success","output":"ok"}'

    def __post_init__(self) -> None:
        # Answers that let a whole run reach its assertions: an address for the
        # Service, and objects carrying exactly the limits FUNCTION declares.
        for fragment, stdout in (
            ("get service control-plane", "10.43.0.7"),
            ("docker inspect", CONTAINER_AT_DECLARED_LIMITS),
            ("get deployment", DEPLOYMENT_AT_DECLARED_LIMITS),
        ):
            self.responses.setdefault(
                fragment,
                TaskResult(task_id="", status="passed", return_code=0, stdout=stdout),
            )

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        rendered = " ".join(task.argv)
        for fragment, response in self.responses.items():
            if fragment in rendered:
                return response
        return TaskResult(
            task_id="", status="passed", return_code=0, stdout=self.default_stdout
        )

    def argv_for(self, fragment: str) -> tuple[str, ...]:
        for spec in self.seen:
            if fragment in " ".join(spec.argv):
                return spec.argv
        raise AssertionError(f"no command matching {fragment!r} in {len(self.seen)} commands")


def _bindings(executor: ScriptedExecutor) -> RoleBindings:
    return RoleBindings(host=executor, stack=executor)


def _ids(workflow: Workflow) -> list[str]:
    return [task.task_id for task in workflow.compile().tasks]


def _titles(workflow: Workflow) -> list[str]:
    return [task.task.title for task in workflow.compile().tasks]


def _request(**changes: object) -> ValidateWorkflowRequest:
    base: dict[str, object] = {"backend": "container", "functions": (FUNCTION,)}
    return ValidateWorkflowRequest(**{**base, **changes})  # pyright: ignore[reportArgumentType]


def test_container_compiles_build_register_invoke_inspect_and_a_release() -> None:
    workflow = build_validate_workflow(_request(), _bindings(ScriptedExecutor()))

    assert _ids(workflow) == [
        "001.build-control-plane",
        "002.build-image-word-stats-java",
        "003.acquire-word-stats-java",
        "004.invoke-word-stats-java",
        "005.inspect-resources-of-nanofaas-word-stats-java-r1",
        "006.release-word-stats-java",
    ]


def test_k8s_adds_the_preflight_the_images_and_the_helm_release() -> None:
    workflow = build_validate_workflow(
        _request(backend="k8s"), _bindings(ScriptedExecutor())
    )

    assert _ids(workflow) == [
        "001.check-kubectl-is-usable",
        "002.build-control-plane",
        "003.build-image-localhost-5000-nanofaas-control-plane-e2e",
        "004.push-image-localhost-5000-nanofaas-control-plane-e2e",
        "005.build-image-word-stats-java",
        "006.push-image-localhost-5000-nanofaas-java-word-stats-e2e",
        "007.acquire-helm-release-nanofaas",
        "008.acquire-word-stats-java",
        "009.invoke-word-stats-java",
        "010.inspect-resources-of-fn-word-stats-java",
        "011.release-word-stats-java",
        "012.release-helm-release-nanofaas",
    ]


def test_k8s_can_add_a_dedicated_queue_probe() -> None:
    workflow = build_validate_workflow(
        _request(backend="k8s", queue_probe=QUEUE_PROBE), _bindings(ScriptedExecutor())
    )

    titles = _titles(workflow)
    assert "Build image k8s-sync-queue" in titles
    assert "Push image localhost:5000/nanofaas/java-warm-echo:e2e" in titles
    assert "Acquire k8s-sync-queue" in titles


def test_the_teardown_is_compiled_in_rather_than_left_to_the_caller() -> None:
    """The legacy workflow exposed cleanup as a separate function the caller had
    to remember; a crash before that call leaked the release and the function."""
    titles = _titles(build_validate_workflow(_request(backend="k8s"), _bindings(ScriptedExecutor())))

    assert titles[-2:] == ["Release word-stats-java", "Release Helm release nanofaas"]


def test_the_control_plane_module_follows_the_backend() -> None:
    for backend, module in (
        ("container", "container-deployment-provider"),
        ("k8s", "k8s-deployment-provider"),
    ):
        executor = ScriptedExecutor()
        build_validate_workflow(_request(backend=backend), _bindings(executor)).run()
        assert f"-PcontrolPlaneModules={module}" in executor.argv_for("bootJar")


def test_additional_modules_join_the_backend_one() -> None:
    executor = ScriptedExecutor()
    build_validate_workflow(
        _request(additional_modules=("autoscaler",)), _bindings(executor)
    ).run()

    assert (
        "-PcontrolPlaneModules=container-deployment-provider,autoscaler"
        in executor.argv_for("bootJar")
    )


def test_buildpack_still_uses_the_dockerfile_pipeline() -> None:
    executor = ScriptedExecutor()
    build_validate_workflow(_request(build="buildpack"), _bindings(executor)).run()

    argv = executor.argv_for("control-plane:boot")
    assert ":control-plane:bootJar" in argv
    assert "-PcontrolPlaneModules=container-deployment-provider" in argv


def test_the_container_backend_curls_the_endpoint_it_was_given() -> None:
    executor = ScriptedExecutor()
    build_validate_workflow(_request(), _bindings(executor)).run()

    assert executor.argv_for("v1/functions/word-stats-java:invoke") == (
        "curl",
        "-fsS",
        "-H",
        "Content-Type: application/json",
        "--data",
        '{"text":"a b"}',
        "http://127.0.0.1:18080/v1/functions/word-stats-java:invoke",
    )


def test_the_k8s_endpoint_is_read_from_the_release_not_spelled_in_advance() -> None:
    """The address exists only once the Service does, so it arrives as a resource
    value. The shell version embedded `$(kubectl ...)` in the URL instead."""
    executor = ScriptedExecutor(
        responses={
            "get service control-plane": TaskResult(
                task_id="", status="passed", return_code=0, stdout="10.43.0.7"
            )
        }
    )

    build_validate_workflow(_request(backend="k8s"), _bindings(executor)).run()

    assert executor.argv_for("v1/functions/word-stats-java:invoke")[-1] == (
        "http://10.43.0.7:8080/v1/functions/word-stats-java:invoke"
    )


def test_a_service_without_an_address_fails_the_acquire_and_uninstalls() -> None:
    """Better than registering against an empty URL: the acquire compensates, so
    the half-installed release does not survive the failure."""
    executor = ScriptedExecutor(
        responses={
            "get service control-plane": TaskResult(
                task_id="", status="passed", return_code=0, stdout="   "
            )
        }
    )

    with pytest.raises(RuntimeError, match="no ClusterIP"):
        build_validate_workflow(_request(backend="k8s"), _bindings(executor)).run()

    assert executor.argv_for("helm uninstall")[:3] == ("helm", "uninstall", "nanofaas")


def test_skipping_the_builds_still_deploys_the_named_image() -> None:
    workflow = build_validate_workflow(
        _request(backend="k8s", build_images=False, control_plane_image="ghcr.io/x/cp:1"),
        _bindings(ScriptedExecutor()),
    )

    titles = _titles(workflow)
    assert not [title for title in titles if title.startswith("Build")]
    assert "Check kubectl is usable" in titles


def test_a_local_control_plane_is_only_built_when_one_is_supplied() -> None:
    started: list[str] = []

    def process() -> Resource[object]:
        return Resource(
            title="Acquire local control plane",
            acquire=lambda _inputs: started.append("up") or object(),
            release=lambda _inputs, _value: started.append("down"),
        )

    workflow = build_validate_workflow(
        _request(),
        _bindings(ScriptedExecutor()),
        control_plane_process=process,  # pyright: ignore[reportArgumentType]
    )
    workflow.run()

    assert started == ["up", "down"]
    assert "Acquire local control plane" in _titles(workflow)


def test_the_inspection_asks_the_backend_not_the_control_plane() -> None:
    """A control plane would report what it was told; this proves the declared
    limits reached the object that actually runs the function."""
    executor = ScriptedExecutor(
        responses={
            "docker inspect": TaskResult(
                task_id="",
                status="passed",
                return_code=0,
                stdout='{"CpuShares":512,"NanoCpus":1000000000,'
                '"MemoryReservation":268435456,"Memory":536870912}',
            )
        }
    )

    build_validate_workflow(_request(), _bindings(executor)).run()

    assert executor.argv_for("docker inspect")[-1] == "nanofaas-word-stats-java-r1"


def test_a_container_that_ignored_the_limits_fails_the_run() -> None:
    executor = ScriptedExecutor(
        responses={
            "docker inspect": TaskResult(
                task_id="",
                status="passed",
                return_code=0,
                stdout='{"CpuShares":2,"NanoCpus":1000000000,'
                '"MemoryReservation":268435456,"Memory":536870912}',
            )
        }
    )

    with pytest.raises(RuntimeError, match="CpuShares"):
        build_validate_workflow(_request(), _bindings(executor)).run()


def test_it_refuses_a_request_with_no_functions() -> None:
    with pytest.raises(ValueError, match="at least one function"):
        _ = ValidateWorkflowRequest(backend="container", functions=())


def test_it_refuses_to_skip_the_builds_without_an_image_to_deploy() -> None:
    with pytest.raises(ValueError, match="control_plane_image is required"):
        _ = ValidateWorkflowRequest(
            backend="k8s", functions=(FUNCTION,), build_images=False
        )


def test_kubernetes_work_runs_on_the_stack_and_container_work_here() -> None:
    assert _request(backend="k8s").role == "stack"
    assert _request(backend="container").role == "host"


def test_every_function_gets_its_own_resource_so_a_slice_keeps_its_own() -> None:
    second = ValidateFunction(
        name="roman-numeral",
        image="localhost:5000/nanofaas/roman:e2e",
        payload='{"n":4}',
        build_argv=("./gradlew", ":functions:java:roman-numeral:bootBuildImage"),
    )
    workflow = build_validate_workflow(
        _request(functions=(FUNCTION, second)), _bindings(ScriptedExecutor())
    )

    # Each function's release lands right after its own last consumer, so the two
    # never coexist: the second is registered only once the first is gone.
    assert _ids(workflow) == [
        "001.build-control-plane",
        "002.build-image-word-stats-java",
        "003.build-image-roman-numeral",
        "004.acquire-word-stats-java",
        "005.invoke-word-stats-java",
        "006.inspect-resources-of-nanofaas-word-stats-java-r1",
        "007.release-word-stats-java",
        "008.acquire-roman-numeral",
        "009.invoke-roman-numeral",
        "010.inspect-resources-of-nanofaas-roman-numeral-r1",
        "011.release-roman-numeral",
    ]


def test_inputs_are_only_readable_where_declared() -> None:
    """A sanity check on the mechanism the k8s endpoint relies on: reading a
    resource a task did not declare is an error, not an empty string."""
    resource: Resource[str] = Resource(
        title="X", acquire=lambda _inputs: "v", release=lambda _inputs, _value: None
    )

    with pytest.raises(Exception):
        _ = TaskInputs.empty().resource(resource)


def _started(workflow: Workflow, executor: ScriptedExecutor) -> list[tuple[str, str | None]]:
    """(task_id, parent_task_id) for every task.started of a real run."""

    class Sink:
        def __init__(self) -> None:
            self.events: list[Any] = []

        def emit(self, event: Any) -> None:
            self.events.append(event)

        def status(self, label: str) -> AbstractContextManager[None]:
            del label
            return nullcontext()

    sink = Sink()
    with bind_workflow_sink(sink):
        workflow.run()
    return [
        (event.task_id, event.parent_task_id)
        for event in sink.events
        if event.kind == "task.started"
    ]


def test_the_helm_acquire_reports_its_three_steps_without_becoming_three_units() -> None:
    """The point of the composite: the plan keeps one line for the release, and a
    run stops being silent through a chart install that takes minutes."""
    executor = ScriptedExecutor()
    workflow = build_validate_workflow(_request(backend="k8s"), _bindings(executor))

    started = _started(workflow, executor)
    unit = "007.acquire-helm-release-nanofaas"

    assert len(workflow.compile().tasks) == 12
    assert [task_id for task_id, parent in started if parent == unit] == [
        f"{unit}/install-helm-release-nanofaas",
        f"{unit}/read-the-control-plane-address",
        f"{unit}/resolve-where-control-plane-answers",
    ]


def test_the_address_travels_between_steps_rather_than_being_read_by_hand() -> None:
    executor = ScriptedExecutor(
        responses={
            "get service control-plane": TaskResult(
                task_id="", status="passed", return_code=0, stdout=" 10.43.9.9 \n"
            )
        }
    )

    build_validate_workflow(_request(backend="k8s"), _bindings(executor)).run()

    # Whitespace trimmed, port applied, and the registration used the result.
    assert executor.argv_for("v1/functions")[-1] == "http://10.43.9.9:8080/v1/functions"


def test_kubernetes_waits_for_the_pod_before_invoking_it() -> None:
    """A live run invoked 0.4s after registering and got POOL_ERROR: Connection
    refused — the Service existed, nothing was listening behind it. Registering
    asks the control plane to create the Deployment; it answers before the pod
    does."""
    executor = ScriptedExecutor()

    build_validate_workflow(_request(backend="k8s"), _bindings(executor)).run()

    commands = [" ".join(spec.argv) for spec in executor.seen]
    appeared = next(i for i, c in enumerate(commands) if "get deployment/fn-word-stats-java" in c)
    rolled_out = next(i for i, c in enumerate(commands) if "rollout status" in c)
    invoked = next(i for i, c in enumerate(commands) if ":invoke" in c)

    assert appeared < rolled_out < invoked


def test_the_container_backend_has_no_deployment_to_wait_for() -> None:
    executor = ScriptedExecutor()

    build_validate_workflow(_request(), _bindings(executor)).run()

    assert not [spec for spec in executor.seen if "rollout" in " ".join(spec.argv)]


def test_waiting_does_not_add_units_to_the_plan() -> None:
    """Readiness shares the register's fate, so it belongs inside the function's
    acquire rather than beside it: a timeout must still delete what register
    created."""
    assert len(build_validate_workflow(_request(backend="k8s"), _bindings(ScriptedExecutor())).compile().tasks) == 12
