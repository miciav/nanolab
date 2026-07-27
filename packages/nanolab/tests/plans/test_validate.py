from dataclasses import dataclass, field
from pathlib import Path

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.validate import (
    _resolve_function,
    _sonata_function,
    build_validate_plan,
)
from sonata_engine import Workflow
from workflow_tasks.execution.bindings import RoleBindings
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult


DEPLOYMENT_PAYLOAD = (
    '{"spec":{"template":{"spec":{"containers":[{"resources":{}}]}}}}'
)


@dataclass
class RecordingExecutor:
    """Answers enough for a whole k8s run: an address for the Service, a
    Deployment payload for the inspection, success for everything else."""

    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        rendered = " ".join(task.argv)
        stdout = '{"status":"success","output":"ok"}'
        if "get service control-plane" in rendered:
            stdout = "10.43.0.7"
        elif "get deployment" in rendered:
            stdout = DEPLOYMENT_PAYLOAD
        return TaskResult(
            task_id=task.task_id, status="passed", return_code=0, stdout=stdout
        )

    def argv_for(self, fragment: str) -> tuple[str, ...]:
        for spec in self.seen:
            if fragment in " ".join(spec.argv):
                return spec.argv
        raise AssertionError(f"no command matching {fragment!r} among {len(self.seen)}")


def _plan(backend: str, **config: object) -> Workflow:
    return build_validate_plan(
        ScenarioConfig.model_validate(
            {
                "workflow": "validate",
                "backend": backend,
                "functions": ["word-stats-java"],
                **config,
            }
        ),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
    )


def _argv(plan: Workflow, fragment: str) -> tuple[str, ...]:
    """The argv of the one compiled task whose title contains `fragment`."""
    matches = [
        task.task.argv  # pyright: ignore[reportAttributeAccessIssue]
        for task in plan.compile().tasks
        if fragment in task.task.title
    ]
    assert len(matches) == 1, f"{fragment!r} matched {len(matches)} tasks"
    return matches[0]


def test_validate_plan_dispatches_k8s_tasks_to_stack_binding() -> None:
    host = RecordingExecutor()
    stack = RecordingExecutor()
    plan = build_validate_plan(
        ScenarioConfig(workflow="validate", backend="k8s", functions=["word-stats-java"]),
        RoleBindings(host=host, stack=stack),
    )

    assert len(plan.compile().tasks) == 12
    assert host.seen == []


def test_validate_plan_selects_the_queue_modules_kubernetes_validation_exercises() -> None:
    build = _argv(_plan("k8s"), "Build control plane")

    assert "-PcontrolPlaneModules=k8s-deployment-provider,async-queue,sync-queue" in build


def test_validate_plan_keeps_container_validation_local() -> None:
    host = RecordingExecutor()
    stack = RecordingExecutor()
    build_validate_plan(
        ScenarioConfig(workflow="validate", backend="container", functions=["word-stats-java"]),
        RoleBindings(host=host, stack=stack),
    )

    assert stack.seen == []


def test_the_container_control_plane_is_a_resource_with_its_own_teardown() -> None:
    """The legacy builder removed this step from the rendered specs by task id and
    inserted a resource back at the same index; it is now simply a resource."""
    ids = [task.task_id for task in _plan("container").compile().tasks]

    assert ids == [
        "001.build-control-plane",
        "002.build-image-word-stats-java",
        "003.acquire-local-control-plane",
        "004.acquire-word-stats-java",
        "005.invoke-word-stats-java",
        "006.inspect-resources-of-nanofaas-word-stats-java-r1",
        "007.release-word-stats-java",
        "008.release-local-control-plane",
    ]


def test_validate_plan_resolves_build_from_the_function_catalog() -> None:
    image_build = _argv(_plan("container"), "Build image word-stats-java")

    assert image_build[0] == "./gradlew"
    assert image_build[1] == ":functions:java:word-stats:bootBuildImage"
    assert image_build[2].startswith("-PfunctionImage=")


def test_validate_plan_propagates_resource_requests_and_limits() -> None:
    """Read off the manifest rather than a compiled task: the register command
    lives inside the function resource's acquire, which is where it belongs."""
    config = ScenarioConfig.model_validate(
        {
            "workflow": "validate",
            "backend": "container",
            "functions": ["word-stats-java"],
            "resources": {
                "word-stats-java": {
                    "requests": {"cpu": 0.25, "memoryMiB": 128},
                    "limits": {"cpu": 1.0, "memoryMiB": 512},
                }
            },
        }
    )

    body = _sonata_function(_resolve_function(config, "word-stats-java")).manifest().json()

    assert '"memoryMiB":128' in body
    assert '"memoryMiB":512' in body


def test_validate_plan_reads_payload_from_the_nanolab_package(tmp_path: Path) -> None:
    tool_root = tmp_path / "nanolab"
    payloads = tool_root / "scenarios" / "payloads"
    payloads.mkdir(parents=True)
    (payloads / "word-stats-sample.json").write_text(
        '{"text": "owned by nanolab", "topN": 1}',
        encoding="utf-8",
    )

    plan = build_validate_plan(
        ScenarioConfig(
            workflow="validate",
            backend="container",
            functions=["word-stats-java"],
        ),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        repo_root=Path("/nanofaas"),
        tool_root=tool_root,
    )

    assert '"text":"owned by nanolab"' in _argv(plan, "Invoke word-stats-java")[-2]


def test_a_kubernetes_run_installs_the_chart_and_registers_against_the_resolved_address() -> None:
    """Run it: the install and the register only exist inside the resources'
    acquires, so nothing about them is visible in the compiled unit list."""
    stack = RecordingExecutor()
    plan = build_validate_plan(
        ScenarioConfig(workflow="validate", backend="k8s", functions=["word-stats-java"]),
        RoleBindings(host=RecordingExecutor(), stack=stack),
    )

    plan.run()

    push = stack.argv_for("docker push")
    install = stack.argv_for("helm upgrade")
    # The chart and the image that was actually pushed come from one source.
    assert f"controlPlane.image.repository={push[-1].rsplit(':', 1)[0]}" in install
    # And the registration used the address read from the Service, not a guess.
    assert stack.argv_for("v1/functions")[-1] == "http://10.43.0.7:8080/v1/functions"
