"""Release benchmark pinning, aggregation, and regression helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import stat
from typing import TYPE_CHECKING, Any

from sonata_engine import Evidence

from nanolab.functions.catalog import resolve_function_definition
from nanolab.release.evidence import receipt_artifacts as _receipt_artifacts
from nanolab.release.metrics import (
    aggregate_runs,
    PerformanceAggregate,
    PerformanceProfile,
    RegressionPolicy,
    evaluate_regression,
    newest_comparable_record,
)
from nanolab.release.build import _registry_digest_map, _write_json
from nanolab.release.model import Amd64ReleasePlan, ArtifactEvidence, digest_path
from sonata_tasks.loadtest.adapters import HttpPrometheusClient

if TYPE_CHECKING:
    from nanolab.plans.release import ReleaseRequest
    from nanolab.release.resources import ReleaseEndpoints

    # Benchmark phases read the same eight fields from the release plan and from
    # the Sonata request; ReleaseRequest may not be imported at runtime (cycle).
    type ReleasePlanLike = Amd64ReleasePlan | ReleaseRequest

LoadtestBuilder = Callable[..., object]


def _native_image(plan: ReleasePlanLike, target_name: str) -> str:
    for cell in plan.image_plan.cells:
        if cell.target.name == target_name and cell.flavor == "native":
            return cell.image
    raise ValueError(f"release image plan has no AMD64 native image for {target_name}")


def _function_target_name(function_key: str) -> str:
    function = resolve_function_definition(function_key)
    prefix = {"exec": "bash", "java-lite": "java-lite"}.get(function.runtime, function.runtime)
    return f"{prefix}-{function.family}"


def _performance_profile(plan: ReleasePlanLike) -> PerformanceProfile:
    azure = plan.environment.azure
    assert azure is not None
    return PerformanceProfile(
        name=plan.settings.profile,
        provider="azure",
        stack_vm=azure.vm_size,
        loadgen_vm=azure.loadgen_vm_size,
        architecture="amd64",
        flavor="native",
        scenario=plan.settings.scenario_name,
    )


def _regression_policy(plan: ReleasePlanLike) -> RegressionPolicy:
    return RegressionPolicy(
        throughput_max_loss_percent=plan.settings.throughput_max_loss_percent,
        p95_max_increase_percent=plan.settings.p95_max_increase_percent,
        error_rate_max=plan.settings.error_rate_max,
    )


def _release_records(directory: Path) -> tuple[Mapping[str, Any], ...]:
    if not directory.is_dir():
        return ()
    return tuple(_read_json_file(path) for path in sorted(directory.glob("*.json")))


def _aggregate_from_record(record: Mapping[str, Any]) -> PerformanceAggregate:
    profile = record["profile"]
    if not isinstance(profile, Mapping):
        raise ValueError("release performance profile must be an object")
    metrics = record["aggregates"]
    if not isinstance(metrics, Mapping):
        raise ValueError("release aggregates must be an object")
    return PerformanceAggregate(
        profile=PerformanceProfile(
            name=str(profile["name"]),
            provider=str(profile["provider"]),
            stack_vm=str(profile["stackVm"]),
            loadgen_vm=str(profile["loadgenVm"]),
            architecture=str(profile["architecture"]),
            flavor=str(profile["flavor"]),
            scenario=str(profile["scenario"]),
        ),
        run_count=int(record["runCount"]),
        metrics={str(name): float(value) for name, value in metrics.items()},
    )


def _aggregate_from_payload(payload: Mapping[str, Any]) -> PerformanceAggregate:
    profile = payload["profile"]
    metrics = payload["metrics"]
    if not isinstance(profile, Mapping) or not isinstance(metrics, Mapping):
        raise ValueError("aggregate evidence is invalid")
    return PerformanceAggregate(
        profile=PerformanceProfile(**{str(key): str(value) for key, value in profile.items()}),
        run_count=int(payload["run_count"]),
        metrics={str(key): float(value) for key, value in metrics.items()},
    )


def _read_json_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON evidence must be an object: {path}")
    return payload


performance_profile = _performance_profile
regression_policy = _regression_policy


def _file_evidence(artifact: ArtifactEvidence) -> Evidence:
    return Evidence("file-digest", artifact.reference, artifact.digest)


def run_sonata_benchmark(
    plan: ReleasePlanLike,
    index: int,
    loadtest_builder: LoadtestBuilder,
    bindings: object,
    fetcher: object | None,
    endpoints: ReleaseEndpoints,
    registry_receipt: Path,
) -> Evidence:
    """Run one isolated load test against the digest-pinned release matrix."""
    digests = _registry_digest_map(
        plan.image_plan,
        _receipt_artifacts(registry_receipt, "local-registry-push", "local-registry-digest"),
    )
    if index < 1:
        raise ValueError("benchmark index must be positive")
    run_dir = _clean_local_run_dir(Path(plan.run_dir), index)
    summary = run_dir / "summary.json"
    role = plan.environment.target("loadgen" if "loadgen" in plan.environment.roles else "stack")
    home = role.home or ("/root" if role.user == "root" else f"/home/{role.user}")
    remote_run_dir = Path(home) / "nanofaas-release" / plan.version / "benchmarks" / f"run-{index}"
    workflow = loadtest_builder(
        plan.scenario,
        plan.environment,
        bindings,
        control_plane_url=endpoints.control_plane,
        prometheus_client=HttpPrometheusClient(endpoints.prometheus),
        run_dir=run_dir,
        remote_run_dir=remote_run_dir,
        fetcher=fetcher,
        repo_root=plan.repo_root,
        prebuilt_control_plane_image=_pinned_native_image_from_evidence(
            plan, "control-plane", digests
        ),
        prebuilt_function_images={
            function: _pinned_native_image_from_evidence(
                plan, _function_target_name(function), digests
            )
            for function in plan.scenario.functions
        },
    )
    workflow.run()  # type: ignore[attr-defined]
    if not summary.is_file():
        raise RuntimeError(f"load-test summary was not written: {summary}")
    return Evidence("file-digest", str(summary), digest_path(summary))


def _clean_local_run_dir(run_root: Path, index: int) -> Path:
    if (
        not run_root.is_absolute()
        or run_root.resolve(strict=True) != run_root
        or ".." in run_root.parts
    ):
        raise ValueError("benchmark run root must be a canonical non-symlink directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(run_root.anchor, flags)
    try:
        for component in run_root.parts[1:]:
            child_fd = os.open(component, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        name = f"run-{index}"
        try:
            target = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(target.st_mode):
                raise ValueError("benchmark run directory must be a real directory")
            shutil.rmtree(name, dir_fd=parent_fd)
        os.mkdir(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    return run_root / name


def _pinned_native_image_from_evidence(
    plan: ReleasePlanLike, target_name: str, digests: Mapping[str, str]
) -> str:
    tagged = _native_image(plan, target_name)
    repository, _ = tagged.rsplit(":", 1)
    return f"{repository}@{digests[tagged]}"


def run_sonata_aggregate(
    plan: ReleasePlanLike,
    benchmark_receipts: tuple[Path, ...],
) -> Evidence:
    summaries = []
    for index, receipt in enumerate(benchmark_receipts, 1):
        expected = plan.run_dir / f"run-{index}" / "summary.json"
        artifacts = _receipt_artifacts(receipt, f"benchmark-{index}", "file-digest")
        if len(artifacts) != 1 or artifacts[0].reference != str(expected):
            raise ValueError(f"invalid benchmark-{index} evidence")
        if digest_path(expected) != artifacts[0].digest:
            raise RuntimeError(f"benchmark-{index} evidence changed")
        summaries.append(_read_json_file(expected))
    aggregate = aggregate_runs(_performance_profile(plan), tuple(summaries))
    return _file_evidence(_write_json(plan.run_dir / "aggregate.json", asdict(aggregate)))


def run_sonata_regression_gate(
    plan: ReleasePlanLike,
    aggregate_receipt: Path | None = None,
) -> Evidence:
    aggregate_path = plan.run_dir / "aggregate.json"
    if aggregate_receipt is not None:
        artifacts = _receipt_artifacts(aggregate_receipt, "aggregate", "file-digest")
        if (
            len(artifacts) != 1
            or artifacts[0].reference != str(aggregate_path)
            or artifacts[0].digest != digest_path(aggregate_path)
        ):
            raise RuntimeError("aggregate evidence changed")
    aggregate = _aggregate_from_payload(_read_json_file(aggregate_path))
    record = newest_comparable_record(
        tuple(
            item
            for item in _release_records(plan.performance_root / "releases")
            if str(item.get("version")).removeprefix("v") != plan.version.removeprefix("v")
        ),
        aggregate.profile,
    )
    baseline = _aggregate_from_record(record) if record is not None else None
    decision = evaluate_regression(
        aggregate,
        baseline,
        _regression_policy(plan),
        k6_passed=True,
        autoscaling_passed=True,
    )
    evidence = _file_evidence(
        _write_json(plan.run_dir / "regression-decision.json", asdict(decision))
    )
    if not decision.passed:
        raise RuntimeError("release regression gate failed: " + "; ".join(decision.failures))
    return evidence
