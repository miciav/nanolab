from dataclasses import dataclass, field
from pathlib import Path

import pytest

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.loadtest import build_loadtest_plan
from workflow_tasks.execution.bindings import RoleBindings
from workflow_tasks.loadtest import autoscaling
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult


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

    def query_range(self, *args, **kwargs):
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
    monkeypatch.setattr(autoscaling.time, "sleep", lambda _seconds: None)


SCENARIO = ScenarioConfig(workflow="loadtest", functions=["word-stats-java"])

# Twelve, not eighteen: the eight steps of the load itself are one composite,
# because none of them can run without what the run before it produced.
DEFAULT_TASK_IDS = [
    "001.check-kubectl-is-usable",
    "002.build-control-plane",
    "003.build-image-localhost-5000-nanofaas-control-plane-e2e",
    "004.push-image-localhost-5000-nanofaas-control-plane-e2e",
    "005.build-application-artifact-word-stats-java",
    "006.build-image-word-stats-java",
    "007.push-image-localhost-5000-nanofaas-java-word-stats-e2e",
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


def test_local_loadtest_reads_k6_script_from_the_nanolab_package(tmp_path: Path) -> None:
    tool_root = tmp_path / "nanolab"
    executor = RecordingExecutor()
    workflow = build_loadtest_plan(
        SCENARIO,
        EnvironmentConfig(provider="local"),
        RoleBindings(host=executor, stack=executor),
        control_plane_url="http://stack:30080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path / "run",
        repo_root=Path("/nanofaas"),
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
    assert ids.index("007.acquire-helm-release-nanofaas") < ids.index("008.acquire-word-stats-java")
    assert ids.index("008.acquire-word-stats-java") < ids.index("009.run-the-load-test")
    # The teardown is compiled in, in reverse, rather than a list the caller runs.
    assert ids[-2:] == ["010.release-word-stats-java", "011.release-helm-release-nanofaas"]


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
