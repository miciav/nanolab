from dataclasses import dataclass, field
from pathlib import Path

import pytest

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.loadtest import build_loadtest_plan
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.loadtest.models import TimeWindow
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        # The platform half resolves the control plane's address before anything
        # can register against it.
        stdout = "10.43.0.7" if "get service control-plane" in " ".join(task.argv) else ""
        return TaskResult(
            task_id=task.task_id, status="passed", return_code=0, stdout=stdout
        )


class NoopPrometheus:
    """One point per query: the snapshot treats a required query with no data as
    a failure, which is right in production and useless in a unit test."""

    def query_range(
        self, expr: str, window: TimeWindow, step_seconds: int = 5
    ) -> list[dict[str, float | str]]:
        del expr, window, step_seconds
        return [{"timestamp": 0.0, "value": 1.0}]


@dataclass
class FakeFetcher:
    """The tests used to pass a bare `object()` because nothing ever called it.
    The load steps only exist inside the composite now, so they have to run."""

    fetched: list[tuple[str, Path]] = field(default_factory=list)

    def fetch_from(self, remote: str, local: Path) -> None:
        self.fetched.append((remote, local))


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The autoscaling verifier polls for real: 90s settle plus 2x24 polls at 5s.
    These tests now run the workflow rather than inspecting it, so without this
    the file takes six and a half minutes to assert on argv."""
    monkeypatch.setattr("sonata_tasks.loadtest.autoscaling.time.sleep", lambda _seconds: None)


SCENARIO = ScenarioConfig(workflow="loadtest", functions=["word-stats-java"])
CONTAINER_SCENARIO = ScenarioConfig(
    workflow="loadtest",
    backend="container",
    functions=["word-stats-java"],
    autoscaling=True,
)

# Twelve, not eighteen: the eight steps of the load itself are one composite,
# because none of them can run without what the run before it produced.
DEFAULT_TASK_IDS = [
    "001.check-kubectl-is-usable",
    "002.build-control-plane",
    "003.build-image-127-0-0-1-5000-nanofaas-control-plane",
    "004.push-image-127-0-0-1-5000-nanofaas-control-plane",
    "005.build-application-artifact-word-stats-java",
    "006.build-image-word-stats-java",
    "007.push-image-127-0-0-1-5000-nanofaas-java-word-stats-e2e",
    "008.acquire-helm-release-nanofaas",
    "009.acquire-word-stats-java",
    "010.run-the-load-test",
    "011.release-word-stats-java",
    "012.release-helm-release-nanofaas",
]


def _ids(workflow) -> list[str]:  # pyright: ignore[reportMissingParameterType]
    return [task.task_id for task in workflow.compile().tasks]


def _run(workflow, executor: "RecordingExecutor") -> list[str]:  # pyright: ignore[reportMissingParameterType]
    """Run it and return the joined argv of every command that reached the executor.

    The load steps live inside a composite, so nothing about them shows up in the
    compiled unit list — only running reveals what they did."""
    try:
        workflow.run()
    except Exception:
        pass
    return [" ".join(spec.argv) for spec in executor.seen]


@pytest.mark.parametrize(
    ("environment", "expected_role", "fetches"),
    [
        (EnvironmentConfig(provider="local"), "stack", False),
        (
            EnvironmentConfig.model_validate(
                {"provider": "multipass", "roles": {"stack": {"name": "stack"}}}
            ),
            "stack",
            True,
        ),
        (
            EnvironmentConfig.model_validate(
                {
                    "provider": "external",
                    "roles": {
                        "stack": {"host": "stack.example"},
                        "loadgen": {"host": "load.example"},
                    },
                }
            ),
            "loadgen",
            True,
        ),
        (
            EnvironmentConfig.model_validate(
                {
                    "provider": "azure",
                    "azure": {"resource_group": "rg", "location": "westeurope"},
                    "roles": {"stack": {"name": "s"}, "loadgen": {"name": "l"}},
                }
            ),
            "loadgen",
            True,
        ),
        (
            EnvironmentConfig.model_validate(
                {
                    "provider": "proxmox",
                    "proxmox": {"host": "pve", "node": "pve1"},
                    "roles": {"stack": {"name": "s"}, "loadgen": {"name": "l"}},
                }
            ),
            "loadgen",
            True,
        ),
    ],
)
def test_provider_contract_selects_role_and_result_transport(
    tmp_path: Path,
    environment: EnvironmentConfig,
    expected_role: str,
    fetches: bool,
) -> None:
    executor = RecordingExecutor()
    workflow = build_loadtest_plan(
        SCENARIO,
        environment,
        RoleBindings(host=executor, stack=executor, loadgen=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        fetcher=FakeFetcher() if fetches else None,
    )

    _ = _run(workflow, executor)
    preflight = next(spec for spec in executor.seen if spec.argv == ("k6", "version"))
    assert preflight.execution_role == expected_role


def test_loadtest_defaults_preserve_task_ids_byte_for_byte(tmp_path: Path) -> None:
    executor = RecordingExecutor()

    workflow = build_loadtest_plan(
        SCENARIO,
        EnvironmentConfig(provider="local"),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
    )

    assert _ids(workflow) == DEFAULT_TASK_IDS


def test_container_loadtest_uses_compose_without_kubernetes(
    tmp_path: Path, nanofaas_root: Path
) -> None:
    executor = RecordingExecutor()

    workflow = build_loadtest_plan(
        CONTAINER_SCENARIO,
        EnvironmentConfig(provider="local"),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://127.0.0.1:8080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        repo_root=nanofaas_root,
    )

    assert _ids(workflow) == [
        "001.acquire-local-registry",
        "002.acquire-docker-compose-project-nanofaas-loadtest",
        "003.build-application-artifact-word-stats-java",
        "004.build-image-word-stats-java",
        "005.push-image-127-0-0-1-5000-nanofaas-java-word-stats-e2e",
        "006.acquire-word-stats-java",
        "007.run-the-load-test",
        "008.release-word-stats-java",
        "009.release-docker-compose-project-nanofaas-loadtest",
        "010.release-local-registry",
    ]


def test_container_loadtest_rejects_remote_environment(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires a local environment"):
        build_loadtest_plan(
            CONTAINER_SCENARIO,
            EnvironmentConfig.model_validate(
                {"provider": "multipass", "roles": {"stack": {"name": "stack"}}}
            ),
            RoleBindings(
                host=RecordingExecutor(),
                stack=RecordingExecutor(),
            ),
            control_plane_url="http://127.0.0.1:8080",
            prometheus_client=NoopPrometheus(),
            run_dir=tmp_path,
        )


def test_dedicated_loadgen_uses_the_staged_nanolab_k6_asset(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {
                "stack": {"host": "stack.example"},
                "loadgen": {"host": "load.example"},
            },
        }
    )
    workflow = build_loadtest_plan(
        SCENARIO,
        environment,
        RoleBindings(host=executor, stack=executor, loadgen=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        fetcher=FakeFetcher(),
    )

    k6 = next(command for command in _run(workflow, executor) if command.startswith("k6 run"))
    assert "/home/ubuntu/nanolab-assets/k6/two-vm-function-invoke.js" in k6


def test_explicit_remote_run_directory_is_cleaned_before_k6(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    fetcher = FakeFetcher()
    remote_run_dir = Path("/home/ubuntu/nanofaas-release/v1.2.3/benchmarks/run-1")
    workflow = build_loadtest_plan(
        SCENARIO,
        EnvironmentConfig.model_validate(
            {
                "provider": "external",
                "roles": {
                    "stack": {"host": "stack.example"},
                    "loadgen": {"host": "load.example"},
                },
            }
        ),
        RoleBindings(host=executor, stack=executor, loadgen=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        remote_run_dir=remote_run_dir,
        fetcher=fetcher,
    )

    commands = _run(workflow, executor)
    cleanup = next(index for index, command in enumerate(commands) if "rm -rf --" in command)
    k6 = next(index for index, command in enumerate(commands) if command.startswith("k6 run"))

    assert cleanup < k6
    assert str(remote_run_dir) in commands[cleanup]
    assert str(remote_run_dir / "k6-summary.json") in commands[k6]
    assert fetcher.fetched == [(str(remote_run_dir / "k6-summary.json"), tmp_path)]


@pytest.mark.parametrize(
    "remote_run_dir",
    (
        Path("/run-1"),
        Path("/home/ubuntu/nanofaas-release/v1.2.3/benchmarks/../run-1"),
        Path("/tmp/nanofaas-release/v1.2.3/benchmarks/run-1"),
        Path("/home/ubuntu/nanofaas-release/v1.2.3"),
        Path("/home/ubuntu/nanofaas-release/benchmarks/run-1"),
    ),
)
def test_remote_cleanup_rejects_paths_outside_release_benchmark_run(
    tmp_path: Path, remote_run_dir: Path
) -> None:
    with pytest.raises(ValueError, match="run-N child"):
        build_loadtest_plan(
            SCENARIO,
            EnvironmentConfig.model_validate(
                {"provider": "multipass", "roles": {"stack": {"name": "stack"}}}
            ),
            RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
            control_plane_url="http://stack:30080",
            prometheus_client=NoopPrometheus(),
            run_dir=tmp_path,
            remote_run_dir=remote_run_dir,
            fetcher=FakeFetcher(),
        )


def test_a_remote_run_dir_must_be_an_absolute_run_child(tmp_path: Path) -> None:
    """The remote run directory is validated by shape; a relative one is refused
    with a message about the directory, not a type error about a boolean."""
    executor = RecordingExecutor()

    with pytest.raises(ValueError, match="absolute run-N child"):
        build_loadtest_plan(
            SCENARIO,
            EnvironmentConfig.model_validate(
                {"provider": "multipass", "roles": {"stack": {"name": "nanofaas-stack"}}}
            ),
            RoleBindings(host=executor, stack=executor),
            control_plane_url="http://stack:30080",
            prometheus_client=NoopPrometheus(),
            run_dir=tmp_path,
            remote_run_dir=Path("nanofaas-release/v1/benchmarks/run-1"),
        )


def test_local_loadtest_reads_k6_script_from_the_nanolab_package(
    tmp_path: Path, nanofaas_root: Path
) -> None:
    tool_root = tmp_path / "nanolab"
    executor = RecordingExecutor()
    workflow = build_loadtest_plan(
        SCENARIO,
        EnvironmentConfig(provider="local"),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path / "run",
        repo_root=nanofaas_root,
        tool_root=tool_root,
    )

    k6 = next(command for command in _run(workflow, executor) if command.startswith("k6 run"))
    assert str(tool_root / "assets/k6/two-vm-function-invoke.js") in k6


def test_loadtest_plan_deploys_exact_prebuilt_images(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    control_plane_image = "localhost:5000/nanofaas/control-plane:v0.18.0-amd64-native"
    function_image = "localhost:5000/nanofaas/java-word-stats:v0.18.0-amd64-native"

    workflow = build_loadtest_plan(
        SCENARIO,
        EnvironmentConfig(provider="local"),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        prebuilt_control_plane_image=control_plane_image,
        prebuilt_function_images={"word-stats-java": function_image},
    )

    assert not [unit for unit in _ids(workflow) if "build" in unit or "push" in unit]

    commands = _run(workflow, executor)
    install = next(command for command in commands if "helm upgrade" in command)
    assert "controlPlane.image.repository=localhost:5000/nanofaas/control-plane" in install
    assert "controlPlane.image.tag=v0.18.0-amd64-native" in install
    register = next(command for command in commands if "/v1/functions" in command)
    assert function_image in register


def test_remote_prebuilt_loadtest_uses_the_staged_chart_path(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    staged_source = Path("/home/azureuser/nanofaas-release/v0.18.3/source")

    workflow = build_loadtest_plan(
        SCENARIO,
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "stack"}}}
        ),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        fetcher=FakeFetcher(),
        prebuilt_control_plane_image="localhost:5000/nanofaas/control-plane:v0.18.3-amd64",
        prebuilt_function_images={"word-stats-java": "localhost:5000/nanofaas/java-word-stats:v0.18.3-amd64"},
        remote_repo_root=staged_source,
    )

    install = next(command for command in _run(workflow, executor) if "helm upgrade" in command)
    assert str(staged_source / "deploy/helm/nanofaas") in install


def test_prebuilt_loadtest_requires_function_images(tmp_path: Path) -> None:
    executor = RecordingExecutor()

    with pytest.raises(
        ValueError,
        match="prebuilt function images are required in prebuilt mode",
    ):
        build_loadtest_plan(
            SCENARIO,
            EnvironmentConfig(provider="local"),
            RoleBindings(host=executor, stack=executor),
            control_plane_url="http://stack:30080",
            prometheus_client=NoopPrometheus(),
            run_dir=tmp_path,
            prebuilt_control_plane_image="localhost:5000/control-plane:v0.18.0",
        )


def test_prebuilt_loadtest_reports_missing_selected_function_images(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor()

    with pytest.raises(
        ValueError,
        match="missing prebuilt function images: word-stats-java",
    ):
        build_loadtest_plan(
            SCENARIO,
            EnvironmentConfig(provider="local"),
            RoleBindings(host=executor, stack=executor),
            control_plane_url="http://stack:30080",
            prometheus_client=NoopPrometheus(),
            run_dir=tmp_path,
            prebuilt_control_plane_image="localhost:5000/control-plane:v0.18.0",
            prebuilt_function_images={},
        )


def test_loadtest_plan_owns_stack_registration_and_cleanup(tmp_path: Path) -> None:
    executor = RecordingExecutor()

    workflow = build_loadtest_plan(
        SCENARIO,
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "stack"}}}
        ),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        fetcher=FakeFetcher(),
    )

    ids = _ids(workflow)
    assert ids.index("008.acquire-helm-release-nanofaas") < ids.index("009.acquire-word-stats-java")
    assert ids.index("009.acquire-word-stats-java") < ids.index("010.run-the-load-test")
    # The teardown is compiled in, in reverse, rather than a list the caller runs.
    assert ids[-2:] == ["011.release-word-stats-java", "012.release-helm-release-nanofaas"]


def test_loadtest_plan_enables_advanced_metrics(tmp_path: Path) -> None:
    executor = RecordingExecutor()

    workflow = build_loadtest_plan(
        SCENARIO,
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "stack"}}}
        ),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        fetcher=FakeFetcher(),
    )

    install = next(
        command for command in _run(workflow, executor) if "helm upgrade" in command
    )
    assert "NANOFAAS_METRICS_PROFILE" in install
    assert "advanced" in install
    # NodePort too: the load generator reaches the control plane from outside.
    assert "controlPlane.service.type=NodePort" in install


def test_autoscaling_loadtest_builds_registers_and_observes_scaler(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    config = ScenarioConfig(
        workflow="loadtest", functions=["word-stats-java"], autoscaling=True
    )

    workflow = build_loadtest_plan(
        config,
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "stack"}}}
        ),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        fetcher=FakeFetcher(),
    )

    commands = _run(workflow, executor)
    build = next(command for command in commands if "gradlew" in command)
    register = next(command for command in commands if "/v1/functions" in command)
    k6 = next(command for command in commands if command.startswith("k6 run"))

    assert (
        "-PcontrolPlaneModules=k8s-deployment-provider,autoscaler,async-queue,sync-queue"
        in build
    )
    for expected in ("scalingConfig", "INTERNAL", "timeoutMs", "30000", "queueSize", "100"):
        assert expected in register
    assert "autoscaling.js" in k6
    # The autoscaling profile ramps up and back down, so the scaler has something
    # to scale down from.
    for stage in ("10s:10", "20s:20", "90s:20", "10s:0"):
        assert stage in k6
    # The verification is a step of the composite, not a unit of its own: it
    # needs the samples the watcher took while k6 ran.
    assert any("get deployment" in command for command in commands)


def test_hpa_autoscaling_loadtest_enables_adapter_and_keeps_one_replica(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "k8s",
            "functions": ["word-stats-java"],
            "autoscaling": True,
            "autoscalingStrategy": "HPA",
        }
    )

    workflow = build_loadtest_plan(
        config,
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "stack"}}}
        ),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        fetcher=FakeFetcher(),
    )

    commands = _run(workflow, executor)
    install = next(command for command in commands if "helm upgrade" in command)
    register = next(command for command in commands if "/v1/functions" in command)

    assert "hpa-metrics-adapter.enabled=true" in install
    assert "hpa-metrics-adapter.metricsRelistInterval=10s" in install
    assert '"strategy":"HPA"' in register
    assert '"minReplicas":1' in register
    assert '"type":"rps"' in register
    assert '"target":"100"' in register
    assert any("get hpa fn-word-stats-java" in command for command in commands)
    assert any(
        "external.metrics.k8s.io" in command and "nanofaas_rps" in command
        for command in commands
    )
    assert any(
        "actuator/prometheus" in command and "api/v1/query?query=function_dispatch_total" in command
        for command in commands
    )


def test_hpa_scale_to_zero_loadtest_registers_a_zero_replica_floor(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "k8s",
            "functions": ["word-stats-java"],
            "autoscaling": True,
            "autoscalingStrategy": "HPA",
            "hpaScaleToZero": True,
        }
    )

    workflow = build_loadtest_plan(
        config,
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "stack"}}}
        ),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        fetcher=FakeFetcher(),
    )

    commands = _run(workflow, executor)
    register = next(command for command in commands if "/v1/functions" in command)

    assert '"strategy":"HPA"' in register
    assert '"minReplicas":0' in register


def test_autoscaling_loadtest_rejects_nonzero_initial_replicas_before_k6(
    tmp_path: Path,
) -> None:
    @dataclass
    class NonZeroReplicaExecutor(RecordingExecutor):
        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            self.seen.append(task)
            command = " ".join(task.argv)
            stdout = (
                "1"
                if "{.spec.replicas}" in command
                else "10.43.0.7"
                if "get service control-plane" in command
                else ""
            )
            return TaskResult(task_id=task.task_id, status="passed", return_code=0, stdout=stdout)

    executor = NonZeroReplicaExecutor()
    workflow = build_loadtest_plan(
        ScenarioConfig(workflow="loadtest", functions=["word-stats-java"], autoscaling=True),
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "stack"}}}
        ),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        fetcher=FakeFetcher(),
    )

    commands = _run(workflow, executor)

    assert not any(command.startswith("k6 run") for command in commands)


def test_internal_autoscaling_waits_for_the_park_at_zero_it_configures(tmp_path: Path) -> None:
    """The INTERNAL strategy gets a replica floor of 0 and must wait for it.

    Without the wait the load starts against a function that was never parked, so
    the wake-from-zero path goes unexercised and the run proves less than it looks
    like it proves.
    """
    executor = RecordingExecutor()
    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "k8s",
            "functions": ["word-stats-java"],
            "autoscaling": True,
        }
    )

    workflow = build_loadtest_plan(
        config,
        EnvironmentConfig.model_validate(
            {"provider": "multipass", "roles": {"stack": {"name": "stack"}}}
        ),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        fetcher=FakeFetcher(),
    )

    commands = _run(workflow, executor)
    register = next(command for command in commands if "/v1/functions" in command)

    assert '"strategy":"INTERNAL"' in register
    assert '"minReplicas":0' in register
    # This exact string lives only in the park step; `spec.replicas` alone also
    # matches the replica probe that runs during verification, so asserting on it
    # would pass with the wait removed.
    assert any("function never parked at zero" in command for command in commands), (
        "the INTERNAL run must wait for the function to park at zero"
    )
    # No HPA object exists on this path, so nothing may wait on its external metric.
    assert not any("external.metrics.k8s.io" in command for command in commands)
    assert not any("describe hpa" in command for command in commands)
