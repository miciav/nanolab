# ruff: noqa: F403, F405
from __future__ import annotations

from ._release_support import *

from nanolab.release import benchmark as release_benchmark
from nanolab.release.resources import ReleaseEndpoints
from nanolab.release import tasks as release_tasks
from nanolab.release.tasks import source_test_task
from sonata_engine import Workflow


def _registry_receipt(plan) -> Path:
    path = plan.run_dir / "local-registry-push.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "phase": "local-registry-push",
                "evidence": [
                    {
                        "kind": "local-registry-digest",
                        "reference": f"docker://{cell.image}",
                        "digest": "sha256:" + "d" * 64,
                    }
                    for cell in plan.image_plan.cells
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_performance_profile_uses_the_tool_relative_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = release_benchmark._performance_profile(_plan(tmp_path, monkeypatch))

    assert profile.name == "azure-d8s-v5+d2s-v5-amd64-native-loadtest-v1"
    assert profile.provider == "azure"
    assert profile.stack_vm == "Standard_D8s_v5"
    assert profile.loadgen_vm == "Standard_D2s_v5"
    assert profile.architecture == "amd64"
    assert profile.flavor == "native"
    assert profile.scenario == "scenarios-v2/autoscaling-cycle-k8s.yaml"


def test_sonata_benchmarks_use_isolated_dirs_and_exact_registry_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    receipt = _registry_receipt(plan)
    calls: list[dict[str, object]] = []

    class Workflow:
        def __init__(self, run_dir: Path):
            self.run_dir = run_dir

        def run(self) -> None:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.run_dir.joinpath("summary.json").write_text(
                json.dumps(_summary(100)), encoding="utf-8"
            )

    def builder(*_args, **kwargs):
        calls.append(kwargs)
        return Workflow(kwargs["run_dir"])

    for index in range(1, 4):
        release_benchmark.run_sonata_benchmark(
            plan,
            index,
            builder,
            object(),
            None,
            ReleaseEndpoints("http://stack:30080", "http://stack:30090"),
            receipt,
        )

    assert [call["run_dir"] for call in calls] == [
        plan.run_dir / "run-1",
        plan.run_dir / "run-2",
        plan.run_dir / "run-3",
    ]
    assert len({call["remote_run_dir"] for call in calls}) == 3
    assert [Path(call["remote_run_dir"]).name for call in calls] == [
        "run-1",
        "run-2",
        "run-3",
    ]
    assert all("@sha256:" in str(call["prebuilt_control_plane_image"]) for call in calls)
    assert all(call["control_plane_url"] == "http://stack:30080" for call in calls)
    assert all(getattr(call["prometheus_client"], "_url") == "http://stack:30090" for call in calls)
    assert all(
        all("@sha256:" in image for image in call["prebuilt_function_images"].values())
        for call in calls
    )


def test_sonata_benchmark_cannot_reuse_a_stale_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    receipt = _registry_receipt(plan)
    run_dir = plan.run_dir / "run-1"
    stale = run_dir / "summary.json"
    metrics = run_dir / "metrics" / "prometheus-snapshot.json"
    metrics.parent.mkdir(parents=True)
    for path in (run_dir / "k6-summary.json", metrics, stale):
        path.write_text(json.dumps(_summary(100)), encoding="utf-8")

    class Workflow:
        def run(self) -> None:
            pass

    with pytest.raises(RuntimeError, match="summary was not written"):
        release_benchmark.run_sonata_benchmark(
            plan,
            1,
            lambda *_args, **_kwargs: Workflow(),
            object(),
            None,
            ReleaseEndpoints("http://stack:30080", "http://stack:30090"),
            receipt,
        )

    assert list(run_dir.iterdir()) == []


def test_sonata_benchmark_rejects_a_symlinked_run_root_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    real_parent = tmp_path / "real-parent"
    real_root = real_parent / "release-run"
    target = real_root / "run-1"
    target.mkdir(parents=True)
    sentinel = target / "keep-me"
    sentinel.write_text("safe", encoding="utf-8")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    unsafe = replace(plan, run_dir=linked_parent / "release-run")
    receipt = _registry_receipt(unsafe)

    with pytest.raises(ValueError, match="canonical non-symlink"):
        release_benchmark.run_sonata_benchmark(
            unsafe,
            1,
            lambda *_args, **_kwargs: pytest.fail("builder must not run"),
            object(),
            None,
            ReleaseEndpoints("http://stack:30080", "http://stack:30090"),
            receipt,
        )

    assert sentinel.read_text(encoding="utf-8") == "safe"


def test_sonata_aggregate_and_passing_gate_return_digest_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    receipts = []
    for index in range(1, 4):
        summary = plan.run_dir / f"run-{index}" / "summary.json"
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text(json.dumps(_summary(100)), encoding="utf-8")
        receipt = plan.run_dir / f"benchmark-{index}.json"
        receipt.write_text(
            json.dumps(
                {
                    "phase": f"benchmark-{index}",
                    "evidence": [
                        {
                            "kind": "file-digest",
                            "reference": str(summary),
                            "digest": digest_path(summary),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        receipts.append(receipt)

    aggregate = release_benchmark.run_sonata_aggregate(plan, tuple(receipts))
    aggregate_receipt = plan.run_dir / "aggregate-receipt.json"
    aggregate_receipt.write_text(
        json.dumps(
            {
                "phase": "aggregate",
                "evidence": [
                    {
                        "kind": aggregate.kind,
                        "reference": aggregate.reference,
                        "digest": aggregate.digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    decision = release_benchmark.run_sonata_regression_gate(plan, aggregate_receipt)

    assert aggregate.kind == decision.kind == "file-digest"
    assert aggregate.digest == digest_path(Path(aggregate.reference))
    assert decision.digest == digest_path(Path(decision.reference))


def test_sonata_regression_gate_writes_failure_and_blocks_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    profile = release_benchmark._performance_profile(plan)
    current = release_benchmark.PerformanceAggregate(
        profile=profile,
        run_count=3,
        metrics={
            "throughputRps": 50.0,
            "latencyP95Ms": 150.0,
            "errorRate": 0.5,
            "queueWaitMeanMs": 1.0,
            "coldStarts": 1.0,
            "peakReplicas": 5.0,
            "cpuMax": 1.0,
            "heapMaxBytes": 1.0,
        },
    )
    baseline = replace(
        current,
        metrics={**current.metrics, "throughputRps": 100.0, "latencyP95Ms": 100.0},
    )
    plan.run_dir.mkdir(parents=True, exist_ok=True)
    plan.run_dir.joinpath("aggregate.json").write_text(
        json.dumps(asdict(current)), encoding="utf-8"
    )
    records = plan.performance_root / "releases"
    records.mkdir(parents=True)
    records.joinpath("0.0.1.json").write_text(
        json.dumps(
            build_release_record(
                version="0.0.1",
                source_commit="b" * 40,
                image_digests={},
                aggregate=baseline,
                policy=release_benchmark._regression_policy(plan),
            )
        ),
        encoding="utf-8",
    )
    reached: list[str] = []
    gate = release_tasks.regression_gate_task(
        identity=plan.identity,
        run_dir=plan.run_dir,
        phase_inputs={"policy": asdict(release_benchmark._regression_policy(plan))},
        work=lambda _inputs: (
            release_benchmark.run_sonata_regression_gate(plan),
        ),
    )
    arm = source_test_task(
        identity=plan.identity,
        run_dir=plan.run_dir,
        phase_inputs={"phase": "arm"},
        work=lambda _inputs: reached.append("arm-or-publish") or (),
    )
    workflow = Workflow("publication-barrier")
    workflow.add(gate)
    workflow.add(arm)

    with pytest.raises(RuntimeError) as failure:
        workflow.run()

    message = str(failure.value)
    assert "throughput loss" in message
    assert "p95 increase" in message
    assert "error rate" in message
    assert reached == []
    assert json.loads(
        plan.run_dir.joinpath("regression-decision.json").read_text(encoding="utf-8")
    )["passed"] is False
