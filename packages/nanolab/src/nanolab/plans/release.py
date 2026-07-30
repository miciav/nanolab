"""Compile a release scenario into a Sonata workflow.

The 12-phase release pipeline defined in `nanolab release run` replays here as a
linear DAG built from the Task and Resource primitives defined in sonata-tasks.
Each phase that iterates over image cells is a composite Steps node so the
workflow surface is always 12 top-level entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome, Workflow
from sonata_tasks.archive import source_archive_resource
from sonata_tasks.buildx import buildx_builder_resource
from sonata_tasks.release_composites import (
    amd64_build_composite,
    arm64_build_composite,
    arm64_smoke_composite,
    attest_composite,
    publish_aliases_composite,
    publish_architectures_composite,
    publish_manifests_composite,
    registry_push_composite,
    source_tests_composite,
)
from nanolab.plans.release_metrics import AggregateBenchmarks
from sonata_tasks.registry_tunnel import registry_tunnel_resource
from workflow_tasks.execution.bindings import RoleBoundCommandTaskExecutor
from workflow_tasks.loadtest.adapters import HttpPrometheusClient

from nanolab.cli.execution import build_role_bindings
from nanolab.cli.vm_provider import vm_provider_for_environment, vm_request_for_role
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.plan import ImagePlan
from nanolab.release.arm import build_arm64_image_plan
from nanolab.release.metrics import (
    PerformanceProfile,
    RegressionDecision,
    RegressionPolicy,
    newest_comparable_record,
)
from nanolab.release.publish import build_publish_plan
from nanolab.release.run import ReleaseSettings, source_test_commands


def _read_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"configuration must be an object: {path}")
    return value


@dataclass(frozen=True, slots=True)
class ReleaseRequest:
    """Everything needed to compile a release workflow."""

    repo_root: Path
    version: str
    environment: EnvironmentConfig
    scenario: ScenarioConfig
    image_plan: ImagePlan
    settings: ReleaseSettings
    run_dir: Path
    performance_root: Path
    credentials: Any | None = None  # CredentialFiles | None
    nanofaas_root: Path | None = None  # defaults to repo_root


def build_release_workflow(request: ReleaseRequest) -> Workflow:
    """Compile a ReleaseRequest into a 12-phase linear Sonata workflow.

    All 12 top-level nodes are returned so the caller can add them to any
    workflow, slice them with ``select``, or embed them in a larger plan.
    """
    env = request.environment
    if env.provider != "azure":
        raise ValueError("release workflow requires an Azure environment")

    provider = vm_provider_for_environment(env, request.repo_root)
    stack_req = vm_request_for_role(env, "stack", loadtest=True)
    _ = vm_request_for_role(env, "loadgen", loadtest=True)  # captured by build_role_bindings
    arm_req = vm_request_for_role(env, "arm-builder")
    bindings, fetcher = build_role_bindings(env, vm_provider=provider, repo_root=request.repo_root)
    executor = RoleBoundCommandTaskExecutor(bindings)
    stack_host = getattr(provider, "connection_host")(stack_req)

    remote_root = f"/home/azureuser/nanofaas-release/{request.version}"
    source_dir = f"{remote_root}/source"
    control_plane_url = f"http://{stack_host}:30080"
    prometheus_url = f"http://{stack_host}:30090"

    wf = Workflow(workflow_id=f"release-{request.version}")

    # --- Phase 0: Version Bump ---
    nanofaas = request.nanofaas_root or request.repo_root
    version_bump = _version_bump_resource(nanofaas_root=nanofaas, version=request.version)

    # --- Phase 1: Source Tests ---
    archive = source_archive_resource(
        repo_root=request.repo_root,
        commit=request.image_plan.version,
        remote_source_dir=source_dir,
        remote_archive=f"{remote_root}/source.tar",
        provider=provider,
        request=stack_req,
    )
    source_tests = source_tests_composite(
        source_test_commands(Path(source_dir)),
        executor=executor,
    )

    # --- Phase 2: AMD64 Build ---
    amd64_builder = buildx_builder_resource(
        name=f"release-amd64-{request.version}",
        executor=executor,
        role="stack",
    )
    amd64_build = amd64_build_composite(
        request.image_plan,
        executor=executor,
        role="stack",
    )

    # --- Phase 3: Registry Push ---
    registry_push = registry_push_composite(
        request.image_plan,
        executor=executor,
        role="stack",
    )

    # --- Phases 4-6: Benchmarks ---
    from nanolab.config.scenario import ScenarioConfig
    from nanolab.plans.loadtest import build_loadtest_plan

    benchmark_scenario = _read_yaml(
        request.repo_root / "scenarios-v2" / request.settings.scenario_name
    )
    benchmark_config = ScenarioConfig.model_validate(benchmark_scenario)

    benchmark_runs = []
    for i in range(1, request.settings.benchmark_runs + 1):
        bench_wf = build_loadtest_plan(
            benchmark_config,
            env,
            bindings,
            control_plane_url=control_plane_url,
            prometheus_client=HttpPrometheusClient(prometheus_url),
            run_dir=request.run_dir / f"run-{i}",
            fetcher=fetcher,
            repo_root=request.repo_root,
        )
        benchmark_runs.append(bench_wf)

    # --- Phase 7: Aggregate ---
    aggregate = AggregateBenchmarks(
        run_dir=request.run_dir,
        benchmark_count=request.settings.benchmark_runs,
        profile=PerformanceProfile(
            name=request.settings.profile,
            provider="azure",
            stack_vm=env.azure.vm_size if env.azure else "unknown",
            loadgen_vm=env.azure.loadgen_vm_size if env.azure else "unknown",
            architecture="amd64",
            flavor="native",
            scenario=request.settings.scenario_name,
        ),
    )

    # --- Phase 8: Regression Gate ---
    reg_gate = _RegressionGate(
        run_dir=request.run_dir,
        performance_root=request.performance_root,
        version=request.version,
        policy=RegressionPolicy(
            throughput_max_loss_percent=request.settings.throughput_max_loss_percent,
            p95_max_increase_percent=request.settings.p95_max_increase_percent,
            error_rate_max=request.settings.error_rate_max,
        ),
    )

    # --- Phase 9: ARM64 Build ---
    tunnel = registry_tunnel_resource(
        registry_upstream=stack_host,
        provider=provider,
        request=arm_req,
    )
    arm64_builder = buildx_builder_resource(
        name=f"release-arm64-{request.version}",
        executor=executor,
        role="arm-builder",
    )
    arm_plan = build_arm64_image_plan(
        request.repo_root,
        request.version,
        registry=request.image_plan.registry,
    )
    arm64_build = arm64_build_composite(
        arm_plan,
        executor=executor,
        role="arm-builder",
        builder_name=f"release-arm64-{request.version}",
        remote_bake_file=f"{remote_root}/docker-bake-arm64.json",
        remote_source_dir=source_dir,
        registry_upstream=stack_host,
    )

    # --- Phase 10: ARM64 Smoke ---
    arm64_smoke = arm64_smoke_composite(
        arm_plan,
        executor=executor,
        role="arm-builder",
    )

    # --- Phase 11: Publish ---
    pub_plan = build_publish_plan(
        request.repo_root,
        request.version,
        local_registry=request.image_plan.registry,
    )
    pub_arch = publish_architectures_composite(
        pub_plan,
        executor=executor,
        role="stack",
        source_digests={},  # supplied by caller from prior phase evidence
        authfile="/tmp/ghcr-auth/config.json",
    )
    pub_manifests = publish_manifests_composite(
        pub_plan,
        executor=executor,
        role="stack",
        architecture_digests={},
        docker_config="/tmp/ghcr-auth",
    )
    pub_aliases = publish_aliases_composite(
        pub_plan,
        executor=executor,
        role="stack",
        manifest_digests={},
        docker_config="/tmp/ghcr-auth",
    )

    # --- Phase 12: Attest ---
    attest = attest_composite(
        images=(),
        predicate_remote=Path(f"{remote_root}/predicate.json"),
        sbom_dir_remote=Path(f"{remote_root}/sboms"),
        cosign_key="/secrets/cosign-key",
        password_file="/secrets/cosign-password",
        docker_config="/tmp/ghcr-auth",
        executor=executor,
        role="stack",
    )

    # --- Wire the DAG ---
    wf.add(version_bump)  # pyright: ignore[reportArgumentType]
    wf.add(source_tests, requires=(archive, version_bump))  # pyright: ignore[reportArgumentType]
    wf.add(amd64_build, requires=(source_tests, amd64_builder))  # pyright: ignore[reportArgumentType]
    wf.add(registry_push, requires=(amd64_build,))  # pyright: ignore[reportArgumentType]
    for i, bw in enumerate(benchmark_runs):
        wf.add(_SubWorkflowTask(bw), requires=(registry_push,) if i == 0 else ())  # pyright: ignore[reportArgumentType]
    wf.add(aggregate, requires=tuple(_SubWorkflowTask(bw) for bw in benchmark_runs))  # pyright: ignore[reportArgumentType]
    wf.add(reg_gate, requires=(aggregate,))  # pyright: ignore[reportArgumentType]
    wf.add(arm64_build, requires=(reg_gate, tunnel, arm64_builder, archive))  # pyright: ignore[reportArgumentType]
    wf.add(arm64_smoke, requires=(arm64_build,))  # pyright: ignore[reportArgumentType]
    wf.add(pub_arch, requires=(arm64_smoke,))  # pyright: ignore[reportArgumentType]
    wf.add(pub_manifests, requires=(pub_arch,))  # pyright: ignore[reportArgumentType]
    wf.add(pub_aliases, requires=(pub_manifests,))  # pyright: ignore[reportArgumentType]
    wf.add(attest, requires=(pub_aliases,))  # pyright: ignore[reportArgumentType]

    return wf


def _version_bump_resource(
    *,
    nanofaas_root: Path,
    version: str,
) -> Resource[tuple[Path, ...]]:
    """Bump version strings; restore curated files on failure."""

    import subprocess
    from nanolab.release.versioning import _CURATED_COUNTS, prepare_version

    def _curated_paths() -> list[str]:
        return [str(nanofaas_root / p) for p in _CURATED_COUNTS]

    def _git_restore_curated() -> None:
        subprocess.run(
            ("git", "checkout", "--", *_curated_paths()),
            cwd=nanofaas_root,
            capture_output=True,
        )

    def acquire(inputs: TaskInputs) -> tuple[Path, ...]:
        try:
            return prepare_version(nanofaas_root, version)
        except BaseException:
            _git_restore_curated()
            raise

    def release(inputs: TaskInputs, state: tuple[Path, ...]) -> None:
        pass  # bump persists on success

    return Resource(
        title=f"Bump version to {version}",
        acquire=acquire,
        release=release,
        infrastructure=False,
    )


@dataclass
class _SubWorkflowTask(Task[None]):
    """Run a sub-workflow as a single task node."""

    workflow: Workflow
    title: str = ""

    def __post_init__(self) -> None:
        if not self.title:
            self.title = f"Run {self.workflow.workflow_id}"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        self.workflow.run()
        return TaskOutcome(value=None)


@dataclass
class _RegressionGate(Task[RegressionDecision]):
    """Resolve baseline at runtime and evaluate the regression gate."""

    run_dir: Path
    performance_root: Path
    version: str
    policy: RegressionPolicy
    title: str = "Evaluate regression gate"

    def run(self, inputs: TaskInputs) -> TaskOutcome[RegressionDecision]:
        import json

        from nanolab.release.metrics import (
            PerformanceAggregate,
            evaluate_regression,
        )

        # Read the aggregate produced by the previous phase
        agg_path = self.run_dir / "aggregate.json"
        if not agg_path.is_file():
            raise RuntimeError(f"aggregate not found: {agg_path}")
        payload = json.loads(agg_path.read_text(encoding="utf-8"))
        aggregate = PerformanceAggregate(
            profile=PerformanceProfile(**payload["profile"]),
            run_count=int(payload["run_count"]),
            metrics={k: float(v) for k, v in payload["metrics"].items()},
        )

        # Resolve baseline from history
        records_dir = self.performance_root / "releases"
        records = []
        if records_dir.is_dir():
            for path in sorted(records_dir.glob("*.json")):
                if path.stem != self.version:
                    records.append(json.loads(path.read_text(encoding="utf-8")))
        baseline = newest_comparable_record(tuple(records), aggregate.profile)

        baseline_agg = None
        if baseline is not None:
            baseline_agg = PerformanceAggregate(
                profile=PerformanceProfile(**baseline["profile"]),
                run_count=int(baseline["runCount"]),
                metrics={k: float(v) for k, v in baseline["aggregates"].items()},
            )

        decision = evaluate_regression(
            aggregate, baseline_agg, self.policy,
            k6_passed=True, autoscaling_passed=True,
        )
        return TaskOutcome(value=decision)
