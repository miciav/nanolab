from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import Resource, TaskInputs, Workflow
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.http_function import HttpFunctionInvokeTask, HttpStatusCheckTask
from sonata_tasks.manifest import FunctionManifest
from sonata_tasks.metrics import PrometheusScrapeCheckTask
from sonata_tasks.offload import (
    OffloadFunction,
    OffloadWorkflowRequest,
    build_offload_workflow,
)

OFFLOADED = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: application/json\r\n"
    "X-NanoFaaS-Offloaded: true\r\n"
    "\r\n"
    '{"status":"success","output":{"words":2}}'
)
LOCAL = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: application/json\r\n"
    "\r\n"
    '{"status":"success","output":{"words":2}}'
)


@dataclass
class ScriptedExecutor:
    stdout: str = ""
    seen: list[CommandTaskSpec] = field(default_factory=list[CommandTaskSpec])

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=self.stdout)


def test_the_manifest_carries_an_offload_policy_when_there_is_one() -> None:
    """The edge registers the same function as LOCAL with a policy; without this
    field the body could not say so."""
    body = FunctionManifest(
        name="word-stats", image="img", execution_mode="LOCAL", offload={"mode": "always"}
    ).body()

    assert body["offload"] == {"mode": "always"}
    assert body["executionMode"] == "LOCAL"


def test_a_manifest_without_a_policy_omits_the_field() -> None:
    assert "offload" not in FunctionManifest(name="word-stats", image="img").body()


def test_an_offloaded_invocation_passes_and_reads_the_body_behind_the_headers() -> None:
    executor = ScriptedExecutor(stdout=OFFLOADED)

    task = HttpFunctionInvokeTask(
        "word-stats",
        payload="{}",
        endpoint="http://edge:18080",
        executor=executor,
        role="host",
        require_header="X-NanoFaaS-Offloaded",
    )
    _ = task.run(TaskInputs.empty())

    # -i, not -f: the headers have to arrive in the same stdout as the body.
    assert "-isS" in executor.seen[0].argv


def test_an_answer_computed_locally_fails_even_though_it_is_a_valid_response() -> None:
    """The point of the invocation is that it was proxied. A perfectly good
    local answer is the failure this catches."""
    task = HttpFunctionInvokeTask(
        "word-stats",
        payload="{}",
        endpoint="http://edge:18080",
        executor=ScriptedExecutor(stdout=LOCAL),
        role="host",
        require_header="X-NanoFaaS-Offloaded",
    )

    with pytest.raises(RuntimeError, match="no X-NanoFaaS-Offloaded header"):
        task.run(TaskInputs.empty())


def test_the_body_is_still_verified_behind_the_headers() -> None:
    offloaded_but_failed = OFFLOADED.replace(
        '{"status":"success","output":{"words":2}}', '{"status":"error"}'
    )
    task = HttpFunctionInvokeTask(
        "word-stats",
        payload="{}",
        endpoint="http://edge:18080",
        executor=ScriptedExecutor(stdout=offloaded_but_failed),
        role="host",
        require_header="X-NanoFaaS-Offloaded",
    )

    with pytest.raises(RuntimeError, match="did not report success"):
        task.run(TaskInputs.empty())


def test_an_invocation_that_asks_for_no_header_is_unchanged() -> None:
    executor = ScriptedExecutor(stdout='{"status":"success","output":1}')

    _ = HttpFunctionInvokeTask(
        "word-stats", payload="{}", endpoint="http://cp:8080", executor=executor, role="host"
    ).run(TaskInputs.empty())

    assert "-fsS" in executor.seen[0].argv


def test_the_scrape_check_names_the_sample_that_was_missing() -> None:
    """Two `grep -F` calls joined by `&&` exited 1 and said nothing: a missing
    metric and an unexpected failure counter looked identical."""
    task = PrometheusScrapeCheckTask(
        url="http://edge:18081/actuator/prometheus",
        executor=ScriptedExecutor(stdout="jvm_memory_used_bytes 1.0\n"),
        role="host",
        expect=('nanofaas_offload_total{function="word-stats",trigger="eager"}',),
    )

    with pytest.raises(RuntimeError, match=r"expected sample not scraped: nanofaas_offload_total"):
        task.run(TaskInputs.empty())


def test_the_scrape_check_names_the_sample_that_should_not_be_there() -> None:
    task = PrometheusScrapeCheckTask(
        url="http://edge:18081/actuator/prometheus",
        executor=ScriptedExecutor(stdout="nanofaas_offload_failure_total 3.0\n"),
        role="host",
        reject=("nanofaas_offload_failure_total",),
    )

    with pytest.raises(RuntimeError, match="present and should not be"):
        task.run(TaskInputs.empty())


def test_the_scrape_check_passes_when_both_halves_hold() -> None:
    task = PrometheusScrapeCheckTask(
        url="http://edge:18081/actuator/prometheus",
        executor=ScriptedExecutor(stdout='nanofaas_offload_total{trigger="eager"} 4.0\n'),
        role="host",
        expect=('nanofaas_offload_total{trigger="eager"}',),
        reject=("nanofaas_offload_failure_total",),
    )

    _ = task.run(TaskInputs.empty())


def test_a_scrape_check_that_asserts_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="expect or reject"):
        _ = PrometheusScrapeCheckTask(
            url="http://edge:18081/actuator/prometheus",
            executor=ScriptedExecutor(),
            role="host",
        )


def test_the_status_check_accepts_the_status_it_was_told_to_expect() -> None:
    executor = ScriptedExecutor(stdout="502")

    task = HttpStatusCheckTask(
        url="http://edge:18080/v1/functions/word-stats:invoke",
        expected_status=502,
        executor=executor,
        role="host",
        payload="{}",
    )
    _ = task.run(TaskInputs.empty())

    argv = executor.seen[0].argv
    # Not -f: a 502 has to be read, not turned into a non-zero exit.
    assert "-f" not in argv
    assert ("-w", "%{http_code}") == argv[argv.index("-w") : argv.index("-w") + 2]


def test_a_working_offload_fails_the_no_fallback_check() -> None:
    """200 here would mean the edge answered it locally, which is the thing this
    workflow exists to rule out."""
    task = HttpStatusCheckTask(
        url="http://edge:18080/v1/functions/word-stats:invoke",
        expected_status=502,
        executor=ScriptedExecutor(stdout="200"),
        role="host",
    )

    with pytest.raises(RuntimeError, match="answered 200, expected 502"):
        task.run(TaskInputs.empty())


FUNCTION = OffloadFunction(
    name="word-stats-java",
    image="localhost:5000/nanofaas/java-word-stats:e2e",
    payload='{"text":"a b"}',
    build_argv=("./gradlew", ":functions:java:word-stats:bootBuildImage"),
)


def _request(**changes: object) -> OffloadWorkflowRequest:
    base: dict[str, object] = {
        "functions": (FUNCTION,),
        "cloud_endpoint": "http://127.0.0.1:19090",
        "edge_endpoint": "http://127.0.0.1:18080",
        "edge_management": "http://127.0.0.1:18081",
    }
    return OffloadWorkflowRequest(**{**base, **changes})  # pyright: ignore[reportArgumentType]


def _plane(name: str):  # pyright: ignore[reportMissingParameterType]
    def factory() -> Resource[None]:
        return Resource(
            title=f"Acquire {name} control plane",
            acquire=lambda _inputs: None,
            release=lambda _inputs, _value: None,
        )

    return factory


@dataclass
class WorkflowExecutor(ScriptedExecutor):
    """Answers each command of a whole offload run plausibly, so a test that
    exercises the assembly is not stopped by the assertions inside it."""

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        rendered = " ".join(task.argv)
        if ":invoke" in rendered and "http_code" in rendered:
            stdout = "502"
        elif ":invoke" in rendered:
            stdout = OFFLOADED
        elif "actuator/prometheus" in rendered:
            stdout = 'nanofaas_offload_total{function="word-stats-java",trigger="eager"} 1.0\n'
        else:
            stdout = ""
        return TaskResult(task_id="", status="passed", return_code=0, stdout=stdout)


def _workflow(executor: ScriptedExecutor) -> Workflow:
    return build_offload_workflow(
        _request(),
        RoleBindings(host=executor, stack=executor),
        cloud=_plane("cloud"),
        edge=_plane("edge"),
    )


def test_it_compiles_two_control_planes_two_registrations_and_three_checks() -> None:
    ids = [task.task_id for task in _workflow(ScriptedExecutor()).compile().tasks]

    assert ids == [
        "001.build-control-plane",
        "002.build-image-word-stats-java",
        "003.acquire-cloud-control-plane",
        "004.acquire-edge-control-plane",
        "005.acquire-word-stats-java-on-the-cloud",
        "006.acquire-word-stats-java-on-the-edge",
        "007.invoke-word-stats-java",
        "008.verify-word-stats-java-was-offloaded-eagerly",
        "009.verify-word-stats-java-has-no-local-fallback",
        "010.release-word-stats-java-on-the-edge",
        "011.release-word-stats-java-on-the-cloud",
        "012.release-edge-control-plane",
        "013.release-cloud-control-plane",
    ]


def test_it_pushes_function_images_when_requested() -> None:
    ids = [
        task.task_id
        for task in build_offload_workflow(
            _request(),
            RoleBindings(host=ScriptedExecutor(), stack=ScriptedExecutor()),
            cloud=_plane("cloud"),
            edge=_plane("edge"),
            push_function_images=True,
        ).compile().tasks
    ]

    assert ids[:3] == [
        "001.build-control-plane",
        "002.build-image-word-stats-java",
        "003.push-image-localhost-5000-nanofaas-java-word-stats-e2e",
    ]


def test_the_no_fallback_check_runs_before_the_registrations_are_released() -> None:
    """It declares the probe's registrations so the compiler keeps them alive
    until it has run. Without that the releases land first and the edge answers
    404 — a different failure that would look like the same one."""
    ids = [task.task_id for task in _workflow(ScriptedExecutor()).compile().tasks]

    assert ids.index("009.verify-word-stats-java-has-no-local-fallback") < ids.index(
        "010.release-word-stats-java-on-the-edge"
    )


def test_the_two_sides_register_the_same_function_differently() -> None:
    """The cloud runs it; the edge holds no implementation and must proxy."""
    cloud = FUNCTION.cloud_manifest().body()
    edge = FUNCTION.edge_manifest().body()

    assert cloud["executionMode"] == "DEPLOYMENT"
    assert "offload" not in cloud
    assert edge["executionMode"] == "LOCAL"
    assert edge["offload"] == {"mode": "always"}
    assert cloud["name"] == edge["name"] == "word-stats-java"


def test_the_build_selects_the_modules_the_hop_needs() -> None:
    executor = WorkflowExecutor()
    _workflow(executor).run()

    build = next(
        " ".join(spec.argv) for spec in executor.seen if "gradlew" in " ".join(spec.argv)
    )
    assert "-PcontrolPlaneModules=offload,container-deployment-provider" in build


def test_it_refuses_a_request_with_no_functions() -> None:
    with pytest.raises(ValueError, match="at least one function"):
        _ = _request(functions=())


def test_a_failed_check_still_deregisters_both_sides_and_stops_both_planes() -> None:
    """The legacy workflow kept the deletes in a cleanup list the caller had to
    remember to run; anything that crashed before that call leaked both
    registrations."""
    stopped: list[str] = []

    def plane(name: str):  # pyright: ignore[reportMissingParameterType]
        return lambda: Resource(
            title=f"Acquire {name} control plane",
            acquire=lambda _inputs: None,
            release=lambda _inputs, _value: stopped.append(name),
        )

    class NeverOffloads(WorkflowExecutor):
        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            result = super().run(task, dry_run=dry_run)
            if ":invoke" in " ".join(task.argv) and "http_code" not in " ".join(task.argv):
                return TaskResult(task_id="", status="passed", return_code=0, stdout=LOCAL)
            return result

    executor = NeverOffloads()
    workflow = build_offload_workflow(
        _request(),
        RoleBindings(host=executor, stack=executor),
        cloud=plane("cloud"),
        edge=plane("edge"),
    )

    with pytest.raises(RuntimeError, match="no X-NanoFaaS-Offloaded header"):
        workflow.run()

    deletes = [" ".join(spec.argv) for spec in executor.seen if "-X DELETE" in " ".join(spec.argv)]
    assert len(deletes) == 2
    assert any("19090" in command for command in deletes)  # cloud
    assert any("18080" in command for command in deletes)  # edge
    # Releases run in reverse, so the edge stops before the cloud it proxied to.
    assert stopped == ["edge", "cloud"]
