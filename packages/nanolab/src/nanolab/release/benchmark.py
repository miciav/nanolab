"""Release benchmark pinning, aggregation, and regression helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanolab.functions.catalog import resolve_function_definition
from nanolab.release.metrics import (
    aggregate_runs,
    PerformanceAggregate,
    PerformanceProfile,
    RegressionDecision,
    RegressionPolicy,
    evaluate_regression,
    newest_comparable_record,
)
from nanolab.release.state import ArtifactEvidence, digest_path
from workflow_tasks.loadtest.adapters import HttpPrometheusClient

if TYPE_CHECKING:
    from nanolab.release.run import Amd64ReleasePlan
    from nanolab.release.state import ReleaseJournal

LoadtestBuilder = Callable[..., object]


def _coordinator():
    from nanolab.release import run

    return run


def _inspect_registry_digest(*args: Any, **kwargs: Any) -> Any:
    from nanolab.release.build import _inspect_registry_digest as inspect

    return inspect(*args, **kwargs)


def _read_verified_local_json(*args: Any, **kwargs: Any) -> Any:
    return _coordinator()._read_verified_local_json(*args, **kwargs)  # noqa: SLF001


def _write_json(*args: Any, **kwargs: Any) -> Any:
    return _coordinator()._write_json(*args, **kwargs)  # noqa: SLF001


def _run_benchmark(
    plan: Amd64ReleasePlan,
    index: int,
    loadtest_builder: LoadtestBuilder,
    bindings: object,
    fetcher: object | None,
    control_plane_url: str,
    prometheus_url: str,
    provider: object,
    request: object,
    expected_registry_digests: Mapping[str, str],
) -> tuple[ArtifactEvidence, ...]:
    run_dir = plan.run_dir / f"run-{index}"
    workflow = loadtest_builder(
        plan.scenario,
        plan.environment,
        bindings,
        control_plane_url=control_plane_url,
        prometheus_client=HttpPrometheusClient(prometheus_url),
        run_dir=run_dir,
        fetcher=fetcher,
        repo_root=plan.repo_root,
        prebuilt_control_plane_image=_pinned_native_image(
            plan,
            "control-plane",
            provider,
            request,
            expected_registry_digests,
        ),
        prebuilt_function_images={
            function: _pinned_native_image(
                plan,
                _function_target_name(function),
                provider,
                request,
                expected_registry_digests,
            )
            for function in plan.scenario.functions
        },
    )
    workflow.run()  # type: ignore[attr-defined]
    summary = run_dir / "summary.json"
    if not summary.is_file():
        raise RuntimeError(f"load-test summary was not written: {summary}")
    return (ArtifactEvidence("local", str(summary), digest_path(summary)),)


def _native_image(plan: Amd64ReleasePlan, target_name: str) -> str:
    for cell in plan.image_plan.cells:
        if cell.target.name == target_name and cell.flavor == "native":
            return cell.image
    raise ValueError(f"release image plan has no AMD64 native image for {target_name}")


def _pinned_native_image(
    plan: Amd64ReleasePlan,
    target_name: str,
    provider: object,
    request: object,
    expected_registry_digests: Mapping[str, str],
) -> str:
    tagged = _native_image(plan, target_name)
    expected = expected_registry_digests[tagged]
    actual = _inspect_registry_digest(provider, request, tagged)
    if actual != expected:
        raise RuntimeError(f"registry image changed before benchmark: {target_name}")
    repository, _ = tagged.rsplit(":", 1)
    return f"{repository}@{expected}"


def _function_target_name(function_key: str) -> str:
    function = resolve_function_definition(function_key)
    prefix = {"exec": "bash", "java-lite": "java-lite"}.get(function.runtime, function.runtime)
    return f"{prefix}-{function.family}"


def _performance_profile(plan: Amd64ReleasePlan) -> PerformanceProfile:
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


def _regression_policy(plan: Amd64ReleasePlan) -> RegressionPolicy:
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


def _decision_from_payload(payload: Mapping[str, Any]) -> RegressionDecision:
    failures = payload.get("failures")
    if not isinstance(failures, list):
        raise ValueError("regression decision evidence is invalid")
    return RegressionDecision(
        passed=bool(payload["passed"]),
        establishes_baseline=bool(payload["establishes_baseline"]),
        failures=tuple(str(value) for value in failures),
    )


def _read_json_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON evidence must be an object: {path}")
    return payload


def _write_aggregate(
    plan: Amd64ReleasePlan,
    journal: ReleaseJournal,
) -> ArtifactEvidence:
    summaries = tuple(
        _read_verified_local_json(
            journal,
            f"benchmark-{index}",
            plan.run_dir / f"run-{index}" / "summary.json",
        )
        for index in range(1, plan.settings.benchmark_runs + 1)
    )
    aggregate = aggregate_runs(_performance_profile(plan), summaries)
    return _write_json(plan.run_dir / "aggregate.json", asdict(aggregate))


def _evaluate_gate(
    plan: Amd64ReleasePlan,
    journal: ReleaseJournal,
) -> tuple[RegressionDecision, ArtifactEvidence]:
    aggregate = _aggregate_from_payload(
        _read_verified_local_json(
            journal,
            "aggregate",
            plan.run_dir / "aggregate.json",
        )
    )
    baseline_record = newest_comparable_record(
        tuple(
            record
            for record in _release_records(plan.performance_root / "releases")
            # a re-release of this version must not use itself as baseline
            if str(record.get("version")) != plan.version
        ),
        aggregate.profile,
    )
    baseline = _aggregate_from_record(baseline_record) if baseline_record is not None else None
    decision = evaluate_regression(
        aggregate,
        baseline,
        _regression_policy(plan),
        k6_passed=True,
        autoscaling_passed=True,
    )
    artifact = _write_json(plan.run_dir / "regression-decision.json", asdict(decision))
    return decision, artifact


performance_profile = _performance_profile
regression_policy = _regression_policy
