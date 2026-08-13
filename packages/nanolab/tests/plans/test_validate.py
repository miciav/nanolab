from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from nanolab.config.scenario import ScenarioConfig
from nanolab.functions.catalog import list_functions
from nanolab.plans.functions import resolve_function, sonata_function
from nanolab.plans.validate import build_validate_plan
from sonata_engine import Workflow
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.registry import docker_registry_resource
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult


DEPLOYMENT_PAYLOAD = (
    '{"spec":{"template":{"spec":{"containers":[{"resources":{}}]}}}}'
)
NANOLAB_ROOT = Path(__file__).resolve().parents[2]
HANDLER_ENVELOPE_FUNCTIONS = (
    "json-transform-exec",
    "json-transform-go",
    "json-transform-java",
    "json-transform-java-lite",
    "json-transform-javascript",
    "json-transform-python",
    "qr-code-exec",
    "qr-code-go",
    "qr-code-java",
    "qr-code-javascript",
    "qr-code-python",
    "roman-numeral-exec",
    "roman-numeral-go",
    "roman-numeral-java",
    "roman-numeral-java-lite",
    "roman-numeral-javascript",
    "roman-numeral-python",
    "word-stats-exec",
    "word-stats-go",
    "word-stats-java",
    "word-stats-java-lite",
    "word-stats-javascript",
    "word-stats-python",
    "handler-envelope-java",
    "handler-envelope-exec",
    "handler-envelope-go",
    "handler-envelope-javascript",
    "handler-envelope-python",
    "binary-envelope-java",
)
EXCLUDED_HANDLER_ENVELOPE_FUNCTIONS = {"figlet-exec", "figlet-java", "mlimage-python"}


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
        elif ":enqueue" in rendered:
            stdout = '{"executionId":"queued-1","status":"queued"}'
        elif "/v1/executions/" in rendered:
            stdout = '{"executionId":"queued-1","status":"success"}'
        elif "/actuator/prometheus" in rendered:
            stdout = (
                'function_enqueue_total{function="word-stats-java"} 1\n'
                'function_success_total{function="word-stats-java"} 1\n'
            )
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

    # The workflow itself validates deploy, invoke and resource propagation.
    assert len(plan.compile().tasks) == 23
    assert host.seen == []


def test_validate_plan_selects_the_queue_modules_kubernetes_validation_exercises() -> None:
    build = _argv(_plan("k8s"), "Build control plane")

    assert "-PcontrolPlaneModules=k8s-deployment-provider,async-queue,sync-queue" in build


def test_kubernetes_validation_runs_the_queue_burst_with_k6() -> None:
    stack = RecordingExecutor()
    plan = build_validate_plan(
        ScenarioConfig(workflow="validate", backend="k8s", functions=["word-stats-java"]),
        RoleBindings(host=RecordingExecutor(), stack=stack),
    )

    plan.run()

    burst = next(spec for spec in stack.seen if spec.argv[:2] == ("k6", "run"))
    assert "k8s-queue-burst.js" in " ".join(burst.argv)
    assert "NANOFAAS_FUNCTION=k8s-sync-queue" in burst.argv


def test_validate_plan_keeps_container_validation_local() -> None:
    host = RecordingExecutor()
    stack = RecordingExecutor()
    build_validate_plan(
        ScenarioConfig(workflow="validate", backend="container", functions=["word-stats-java"]),
        RoleBindings(host=host, stack=stack),
    )

    assert stack.seen == []


def test_handler_envelope_validation_adds_contract_tasks_for_each_selected_function() -> None:
    plan = build_validate_plan(
        ScenarioConfig.model_validate(
            {
                "workflow": "validate",
                "backend": "container",
                "functions": list(HANDLER_ENVELOPE_FUNCTIONS),
                "handler_envelope": True,
            }
        ),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
    )

    titles = [task.task.title for task in plan.compile().tasks]
    assert "Verify qr-code-java HTTP envelope" in titles
    assert "Verify qr-code-python HTTP envelope" in titles
    assert "Verify handler-envelope HTTP envelope" in titles
    assert "Verify handler-envelope-python HTTP envelope" in titles
    assert "Verify binary-envelope-java HTTP envelope" in titles
    assert "Verify word-stats-java HTTP envelope" in titles


def test_handler_envelope_validation_requires_every_contract_function() -> None:
    with pytest.raises(ValueError, match="handler envelope requires qr-code-java"):
        build_validate_plan(
            ScenarioConfig.model_validate(
                {
                    "workflow": "validate",
                    "backend": "container",
                    "functions": [
                        key for key in HANDLER_ENVELOPE_FUNCTIONS if key != "qr-code-java"
                    ],
                    "handler_envelope": True,
                }
            ),
            RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        )


def test_handler_envelope_container_scenario_runs_every_deterministic_function() -> None:
    config = ScenarioConfig.model_validate(
        yaml.safe_load((NANOLAB_ROOT / "scenarios-v2/handler-envelope-container.yaml").read_text())
    )
    plan = build_validate_plan(
        config,
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
    )

    assert config.backend == "container"
    assert config.functions == list(HANDLER_ENVELOPE_FUNCTIONS)
    titles = [task.task.title for task in plan.compile().tasks]
    assert titles.count("Invoke handler-envelope") == 1
    qr_invocation = next(
        task.task.argv  # pyright: ignore[reportAttributeAccessIssue]
        for task in plan.compile().tasks
        if task.task.title == "Invoke qr-code-java"
    )
    assert qr_invocation[-2] == (
        '{"input":{"text":"https://example.org/invite/abc","size":256}}'
    )
    for key in HANDLER_ENVELOPE_FUNCTIONS:
        assert titles.count(f"Invoke {resolve_function(config, key).name}") == 1


def test_handler_envelope_container_excludes_only_nondeterministic_catalog_functions(
    nanofaas_checkout: Path,
) -> None:
    config = ScenarioConfig.model_validate(
        yaml.safe_load((NANOLAB_ROOT / "scenarios-v2/handler-envelope-container.yaml").read_text())
    )

    example_keys = {
        function.key
        for function in list_functions(nanofaas_checkout)
        if function.example_dir is not None
    }

    assert set(config.functions) == example_keys - EXCLUDED_HANDLER_ENVELOPE_FUNCTIONS


def test_handler_envelope_contracts_send_real_header_and_binary_sentinels() -> None:
    config = ScenarioConfig.model_validate(
        {
            "workflow": "validate",
            "backend": "container",
            "functions": list(HANDLER_ENVELOPE_FUNCTIONS),
            "handler_envelope": True,
        }
    )
    plan = build_validate_plan(
        config, RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor())
    )

    probe = _argv(plan, "Verify handler-envelope HTTP envelope")
    qr = _argv(plan, "Verify qr-code-java HTTP envelope")
    roman = _argv(plan, "Verify roman-numeral-java HTTP envelope")
    transform = _argv(plan, "Verify json-transform-java HTTP envelope")
    assert probe[-2] == (
        '{"input":{"message":"body-sentinel"},"headers":{"x-e2e-token":"forged"}}'
    )
    assert "X-E2E-Token: header-sentinel" in probe
    assert qr[-2] == '{"input":{"text":"https://example.org/invite/abc","size":256}}'
    assert roman[-2] == '{"input":{}}'
    assert transform[-2] == '{"input":{}}'
    plain = _argv(plan, "Verify word-stats-java HTTP envelope")
    assert plain[-2] == sonata_function(resolve_function(config, "word-stats-java")).payload
    for key in ("handler-envelope-exec", "handler-envelope-go", "handler-envelope-java", "handler-envelope-javascript", "handler-envelope-python"):
        probe = _argv(plan, f"Verify {resolve_function(config, key).name} HTTP envelope")
        assert probe[-2] == '{"input":{"message":"body-sentinel"},"headers":{"x-e2e-token":"forged"}}'
        assert "X-E2E-Token: header-sentinel" in probe
    binary = _argv(plan, "Verify binary-envelope-java HTTP envelope")
    assert binary[-2] == '{"input":{}}'


def test_validate_plan_builds_java_lite_with_its_native_dockerfile() -> None:
    image_build = _argv(
        _plan("container", functions=["word-stats-java-lite"]),
        "Build image word-stats-java-lite",
    )

    assert image_build[-2:] == ("functions/java/word-stats-lite/Dockerfile", ".")


def test_container_validation_builds_and_deploys_the_control_plane_with_compose() -> None:
    ids = [task.task_id for task in _plan("container").compile().tasks]

    assert ids == [
        "001.acquire-local-registry",
        "002.acquire-docker-compose-project-nanofaas-validate",
        "003.build-application-artifact-word-stats-java",
        "004.build-image-word-stats-java",
        "005.push-image-127-0-0-1-5000-nanofaas-java-word-stats-e2e",
        "006.acquire-word-stats-java",
        "007.invoke-word-stats-java",
        "008.inspect-resources-of-nanofaas-word-stats-java-r1",
        "009.release-word-stats-java",
        "010.release-docker-compose-project-nanofaas-validate",
        "011.release-local-registry",
    ]


def test_container_validation_owns_an_isolated_compose_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nanolab.plans.validate.docker_registry_resource",
        lambda **kwargs: docker_registry_resource(**kwargs, ready=lambda: True),
    )
    host = RecordingExecutor()
    plan = build_validate_plan(
        ScenarioConfig(
            workflow="validate",
            backend="container",
            functions=["word-stats-java"],
        ),
        RoleBindings(host=host, stack=RecordingExecutor()),
    )

    plan.run()

    assert host.argv_for("docker compose")[:6] == (
        "docker",
        "compose",
        "-f",
        "deploy/compose/compose.yaml",
        "-p",
        "nanofaas-validate",
    )


def test_validate_plan_resolves_build_from_the_function_catalog() -> None:
    artifact_build = _argv(_plan("container"), "Build application artifact: word-stats-java")
    image_build = _argv(_plan("container"), "Build image word-stats-java")

    assert artifact_build[:2] == ("./gradlew", ":functions:java:word-stats:bootJar")
    assert image_build[:2] == ("docker", "build")


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

    body = sonata_function(resolve_function(config, "word-stats-java")).manifest().json()

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
