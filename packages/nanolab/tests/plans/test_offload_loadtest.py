from dataclasses import dataclass, field
import json
import os
from pathlib import Path

import pytest
import yaml

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans import offload_loadtest as offload_loadtest_plan
from nanolab.plans.offload_loadtest import (
    EvaluateOffloadConservation,
    build_offload_loadtest_plan,
)
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

NANOFAAS_ROOT = Path(os.environ["NANOFAAS_ROOT"]).resolve()
NANOLAB_ROOT = Path(__file__).resolve().parents[2]

SCENARIO = ScenarioConfig(
    workflow="offload-loadtest",
    backend="k8s",
    functions=["word-stats-java", "json-transform-java"],
)


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        # Each platform resolves its control plane's address before anything can
        # register against it.
        stdout = "10.43.0.7" if "get service control-plane" in " ".join(task.argv) else ""
        return TaskResult(
            task_id=task.task_id, status="passed", return_code=0, stdout=stdout
        )


def _local_environment() -> EnvironmentConfig:
    return EnvironmentConfig(provider="local")


def _external_environment() -> EnvironmentConfig:
    return EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {
                "stack": {"host": "edge.example"},
                "cloud": {"host": "cloud.example"},
                "loadgen": {"host": "loadgen.example"},
            },
        }
    )


def _bindings(executor: RecordingExecutor) -> RoleBindings:
    return RoleBindings(host=executor, stack=executor, loadgen=executor, cloud=executor)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conservation check scrapes two Prometheus endpoints over urllib. These
    tests run the workflow rather than inspecting it, so without this each one
    waits out two connection timeouts — the file took two minutes."""
    monkeypatch.setattr(
        offload_loadtest_plan, "_fetch_text", lambda _url, **_kwargs: ""
    )


def _run(workflow, executor: RecordingExecutor) -> list[CommandTaskSpec]:  # pyright: ignore[reportMissingParameterType]
    """Run it and return every spec that reached the executor.

    The load steps live inside a composite, so nothing about them appears in the
    compiled unit list — only running reveals what they did."""
    try:
        workflow.run()
    except Exception:
        pass
    return executor.seen


def test_rejects_non_offload_loadtest_scenario() -> None:
    with pytest.raises(ValueError, match="offload-loadtest"):
        build_offload_loadtest_plan(
            ScenarioConfig(workflow="loadtest", functions=["word-stats-java"]),
            _local_environment(),
            _bindings(RecordingExecutor()),
            run_dir=Path("/tmp/run"),
        )


def test_rejects_wrong_function_count() -> None:
    with pytest.raises(ValueError, match="exactly two functions"):
        build_offload_loadtest_plan(
            ScenarioConfig(
                workflow="offload-loadtest", backend="k8s", functions=["word-stats-java"]
            ),
            _local_environment(),
            _bindings(RecordingExecutor()),
            run_dir=Path("/tmp/run"),
        )


def test_deployment_then_registration_then_k6_then_evaluation_ordering(tmp_path: Path) -> None:
    workflow = build_offload_loadtest_plan(
        SCENARIO,
        _local_environment(),
        _bindings(RecordingExecutor()),
        run_dir=tmp_path,
        repo_root=NANOFAAS_ROOT,
    )

    ids = [task.task_id for task in workflow.compile().tasks]

    # The cloud is up before the edge, so the edge's chart comes up pointing at
    # something that answers. k6 and the reconciliation are steps of one unit,
    # so the plan names the load test rather than its internals.
    cloud = next(task for task in ids if task.endswith("acquire-helm-release-nanofaas-on-the-cloud"))
    edge = next(task for task in ids if task.endswith("acquire-helm-release-nanofaas-on-the-edge"))
    function = next(task for task in ids if task.endswith("acquire-word-stats-java-on-the-edge"))
    load = next(task for task in ids if task.endswith("run-the-offload-load-test"))
    assert ids.index(cloud) < ids.index(edge) < ids.index(function) < ids.index(load)


def test_local_provider_runs_k6_on_stack_without_fetch(tmp_path: Path) -> None:
    tool_root = tmp_path / "nanolab"
    executor = RecordingExecutor()
    workflow = build_offload_loadtest_plan(
        SCENARIO,
        _local_environment(),
        _bindings(executor),
        run_dir=tmp_path,
        repo_root=NANOFAAS_ROOT,
        tool_root=tool_root,
    )

    commands = _run(workflow, executor)
    preflight = next(spec for spec in commands if spec.argv == ("k6", "version"))
    k6 = next(" ".join(spec.argv) for spec in commands if spec.argv[0] == "k6" and "run" in spec.argv)

    assert preflight.execution_role == "stack"
    assert str(tool_root / "assets/k6/offload-mixed.js") in k6


def test_dedicated_loadgen_runs_k6_on_loadgen_and_fetches_results(tmp_path: Path) -> None:
    fetched: list[tuple[str, Path]] = []

    class Fetcher:
        def fetch_from(self, remote: str, local: Path) -> None:
            fetched.append((remote, local))

    executor = RecordingExecutor()
    workflow = build_offload_loadtest_plan(
        SCENARIO,
        _external_environment(),
        _bindings(executor),
        run_dir=tmp_path,
        repo_root=NANOFAAS_ROOT,
        fetcher=Fetcher(),
    )

    commands = _run(workflow, executor)
    preflight = next(spec for spec in commands if spec.argv == ("k6", "version"))
    k6_spec = next(spec for spec in commands if spec.argv[0] == "k6" and "run" in spec.argv)
    k6 = " ".join(k6_spec.argv)

    assert preflight.execution_role == "loadgen"
    assert "/home/ubuntu/nanolab-assets/k6/offload-mixed.js" in k6
    assert "OFFLOADABLE_RATE=100" in k6_spec.argv
    # A remote load generator writes its summary there, so it has to come back.
    assert fetched == [("/home/ubuntu/nanofaas-loadtest/k6-summary.json", tmp_path)]


def test_dedicated_loadgen_requires_a_fetcher(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fetcher is required"):
        build_offload_loadtest_plan(
            SCENARIO,
            _external_environment(),
            _bindings(RecordingExecutor()),
            run_dir=tmp_path,
            repo_root=NANOFAAS_ROOT,
        )


def test_evaluation_rejects_a_run_without_offloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary_path = tmp_path / "k6-summary.json"
    summary_path.write_text(
        '{"metrics":{"offloadable_requests":{"count":601},"offloaded_requests":{"count":0}}}',
        encoding="utf-8",
    )
    report_path = tmp_path / "offload-report.json"
    monkeypatch.setattr(
        offload_loadtest_plan,
        "_fetch_text",
        lambda url, **_kwargs: (
            'function_success_total{function="word-stats-java"} 601\n'
            if url == "http://edge/metrics"
            else ""
        ),
    )

    with pytest.raises(RuntimeError, match="no requests were offloaded"):
        EvaluateOffloadConservation(
            task_id="",
            title="Evaluate offload conservation",
            k6_summary_path=summary_path,
            edge_metrics_url="http://edge/metrics",
            cloud_metrics_url="unused",
            offloadable="word-stats-java",
            control="json-transform-java",
            output_path=report_path,
        ).run()

    assert json.loads(report_path.read_text(encoding="utf-8"))["passed"] is False


def test_summary_does_not_describe_nonzero_offload_as_zero() -> None:
    summary = offload_loadtest_plan.format_offload_summary(
        {
            "k6_offloadable_requests": 601,
            "edge_function_success_offloadable": 601,
            "k6_offloaded_requests": 12,
            "edge_offload_total": 12,
            "cloud_function_success_offloadable": 12,
        }
    )

    assert "did not offload" not in summary
    assert "did not process" not in summary


def test_edge_offload_target_points_at_the_cloud_role(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    workflow = build_offload_loadtest_plan(
        SCENARIO,
        _external_environment(),
        _bindings(executor),
        run_dir=tmp_path,
        repo_root=NANOFAAS_ROOT,
        fetcher=object(),
    )

    installs = [
        " ".join(spec.argv) for spec in _run(workflow, executor) if "helm upgrade" in " ".join(spec.argv)
    ]
    # Only the edge's chart is told where to offload to; the cloud is the target.
    assert sum("NANOFAAS_OFFLOAD_TARGETURL" in command for command in installs) == 1
    edge_install = next(command for command in installs if "NANOFAAS_OFFLOAD_TARGETURL" in command)
    assert "].value=http://cloud.example:30080" in edge_install
    assert "SYNC_QUEUE_MAX_DEPTH" in edge_install
    assert "controlPlane.extraEnv[5].value=1 --set" in edge_install


def test_cleanup_covers_both_control_planes(tmp_path: Path) -> None:
    workflow = build_offload_loadtest_plan(
        SCENARIO,
        _local_environment(),
        _bindings(RecordingExecutor()),
        run_dir=tmp_path,
        repo_root=NANOFAAS_ROOT,
    )

    ids = [task.task_id for task in workflow.compile().tasks]

    # Compiled in, in reverse, rather than a list the runner has to remember —
    # and the control function exists only on the edge.
    assert [task.split(".", 1)[1] for task in ids[-5:]] == [
        "release-json-transform-java-on-the-edge",
        "release-word-stats-java-on-the-edge",
        "release-helm-release-nanofaas-on-the-edge",
        "release-word-stats-java-on-the-cloud",
        "release-helm-release-nanofaas-on-the-cloud",
    ]


def test_scenario_file_parses_with_two_ordered_functions() -> None:
    payload = yaml.safe_load(
        (NANOLAB_ROOT / "scenarios-v2/offload-loadtest.yaml").read_text()
    )
    config = ScenarioConfig.model_validate(payload)
    assert config.workflow == "offload-loadtest"
    assert config.backend == "k8s"
    assert config.functions == ["word-stats-java", "json-transform-java"]
