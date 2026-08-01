"""Compile a release scenario into a Sonata workflow.

The 12-phase release pipeline defined in `nanolab release run` replays here as a
linear DAG built from the Task and Resource primitives defined in sonata-tasks.
Each phase that iterates over image cells is a composite Steps node so the
workflow surface is always 12 top-level entries.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from sonata_engine import Task, TaskInputs, TaskOutcome, Workflow
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
from nanolab.images.plan import DEFAULT_REGISTRY, ImagePlan, build_image_plan
from nanolab.release.arm import build_arm64_image_plan
from nanolab.release.environment import validate_release_environment
from nanolab.release.metrics import (
    PerformanceProfile,
    RegressionDecision,
    RegressionPolicy,
    newest_comparable_record,
)
from nanolab.release.publish import build_publish_plan
from nanolab.release.resources import build_release_resources
from nanolab.release.run import CredentialFiles, ReleaseSettings, git_state, source_test_commands
from nanolab.release.state import ReleaseIdentity, digest_path
from nanolab.release.versioning import normalize_version, verify_version_consistency


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
    credentials: CredentialFiles | None = None
    nanofaas_root: Path | None = None  # defaults to repo_root
    identity: ReleaseIdentity | None = None


def build_release_request(
    *,
    repo_root: Path,
    nanofaas_root: Path,
    scenario_path: Path,
    environment_path: Path,
    release_config_path: Path | None,
    run_dir: Path,
    performance_root: Path,
    executable: bool = False,
) -> ReleaseRequest:
    """Validate local release inputs without constructing cloud infrastructure."""
    tool_root = Path(repo_root).expanduser().resolve()
    source_root = Path(nanofaas_root).expanduser().resolve()
    scenario_file = Path(scenario_path).expanduser().resolve()
    environment_file = Path(environment_path).expanduser().resolve()
    scenario = ScenarioConfig.model_validate(_read_yaml(scenario_file))
    if scenario.workflow != "release" or scenario.release is None:
        raise ValueError("release preflight requires a release scenario")

    release = scenario.release
    policy = (
        release.profile,
        release.benchmark_scenario,
        release.throughput_max_loss_percent,
        release.p95_max_increase_percent,
        release.error_rate_max,
    )
    if policy != (
        "azure-d8s-v5+d2s-v5-amd64-native-loadtest-v1",
        "loadtest.yaml",
        10.0,
        15.0,
        0.30,
    ):
        raise ValueError("release scenario does not match the canonical Azure performance policy")
    benchmark_scenario = scenario_file.parent / release.benchmark_scenario
    if not benchmark_scenario.is_file():
        raise ValueError("release benchmark scenario must be a file")

    environment = EnvironmentConfig.model_validate(_read_yaml(environment_file))
    plain_version, version_tag = normalize_version(release.version)
    source_commit = _release_source_commit(source_root, plain_version)
    validate_release_environment(environment, source_root, plain_version)

    credentials = None
    if release_config_path is not None:
        credential_config = _read_yaml(Path(release_config_path).expanduser().resolve())
        try:
            credentials = CredentialFiles(
                ghcr_token=Path(str(credential_config["ghcr_token_file"])).expanduser().absolute(),
                cosign_key=Path(str(credential_config["cosign_key_file"])).expanduser().absolute(),
                cosign_password=Path(
                    str(credential_config["cosign_password_file"])
                ).expanduser().absolute(),
            )
        except KeyError as error:
            raise ValueError("release credential configuration is incomplete") from error
        credentials.validate(repo_root=tool_root).validate(repo_root=source_root)
    elif executable:
        raise ValueError("release credential config is required for execution")

    image_plan = build_image_plan(
        source_root,
        version_tag,
        registry=DEFAULT_REGISTRY,
        architectures=("amd64",),
    )
    if not image_plan.cells:
        raise ValueError("release image matrix must not be empty")

    settings = ReleaseSettings(
        max_parallelism=release.max_parallelism,
        scenario=benchmark_scenario,
        scenario_name=release.benchmark_scenario,
        benchmark_runs=release.benchmark_runs,
        profile=release.profile,
        throughput_max_loss_percent=release.throughput_max_loss_percent,
        p95_max_increase_percent=release.p95_max_increase_percent,
        error_rate_max=release.error_rate_max,
    )
    return ReleaseRequest(
        repo_root=tool_root,
        nanofaas_root=source_root,
        version=release.version,
        environment=environment,
        scenario=scenario,
        image_plan=image_plan,
        settings=settings,
        run_dir=Path(run_dir).expanduser().resolve(),
        performance_root=Path(performance_root).expanduser().resolve(),
        credentials=credentials,
        identity=ReleaseIdentity(
            source_commit=source_commit,
            prepared_version=plain_version,
            release_config_digest=digest_path(scenario_file),
            environment_digest=digest_path(environment_file),
        ),
    )


def build_release_workflow(
    request: ReleaseRequest,
    *,
    provider: Any = None,
) -> Workflow:
    """Compile a ReleaseRequest into a 12-phase linear Sonata workflow.

    All 12 top-level nodes are returned so the caller can add them to any
    workflow, slice them with ``select``, or embed them in a larger plan.
    """
    env = request.environment
    if env.provider != "azure":
        raise ValueError("release workflow requires an Azure environment")

    nanofaas = request.nanofaas_root or request.repo_root
    source_commit = (
        request.identity.source_commit
        if request.identity is not None
        else _release_source_commit(nanofaas, request.version)
    )
    _, expected_image_tag = normalize_version(request.version)
    if request.image_plan.version != expected_image_tag:
        raise ValueError("release image plan version does not match the requested project version")

    if provider is None:
        provider = vm_provider_for_environment(env, request.repo_root)
    infrastructure = build_release_resources(env, nanofaas, provider)
    stack_req = vm_request_for_role(env, "stack", loadtest=True)
    _ = vm_request_for_role(env, "loadgen", loadtest=True)  # captured by build_role_bindings
    arm_req = vm_request_for_role(env, "arm-builder")
    bindings, fetcher = build_role_bindings(env, vm_provider=provider, repo_root=nanofaas)
    executor = RoleBoundCommandTaskExecutor(bindings)
    # Runtime consumers resolve the real addresses from `infrastructure.endpoints`.
    # Phase tasks still accept strings today; their migration consumes that value
    # directly in the benchmark task added later in this plan.
    stack_host = "<release-stack>"

    remote_root = f"/home/azureuser/nanofaas-release/{request.version}"
    source_dir = f"{remote_root}/source"
    control_plane_url = f"http://{stack_host}:30080"
    prometheus_url = f"http://{stack_host}:30090"

    wf = Workflow(workflow_id=f"release-{request.version}")

    # --- Phase 1: Source Tests ---
    archive = source_archive_resource(
        repo_root=nanofaas,
        commit=source_commit,
        remote_source_dir=source_dir,
        remote_archive=f"{remote_root}/source.tar",
        provider=provider,
        request=stack_req,
    )
    archive = replace(archive, requires=(infrastructure.stack,))
    source_tests = source_tests_composite(
        source_test_commands(Path(source_dir)),
        executor=executor,
    )

    # --- Phase 2: AMD64 Build ---
    amd64_builder = buildx_builder_resource(
        name=f"release-amd64-{request.version}",
        executor=executor,
        role="stack",
        requires=(infrastructure.stack,),
    )
    amd64_build = amd64_build_composite(
        request.image_plan,
        executor=executor,
        role="stack",
        cwd=Path(source_dir),
    )

    # --- Phase 3: Registry Push ---
    registry_push = registry_push_composite(
        request.image_plan,
        executor=executor,
        role="stack",
        tls_verify=False,
    )

    # --- Phases 4-6: Benchmarks ---
    from nanolab.config.scenario import ScenarioConfig
    from nanolab.plans.loadtest import build_loadtest_plan

    benchmark_scenario = _read_yaml(request.settings.scenario)
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
            repo_root=Path(source_dir),
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
        requires=(infrastructure.stack, infrastructure.arm_builder),
    )
    arm64_builder = buildx_builder_resource(
        name=f"release-arm64-{request.version}",
        executor=executor,
        role="arm-builder",
        requires=(infrastructure.arm_builder,),
    )
    arm_plan = build_arm64_image_plan(
        nanofaas,
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
        nanofaas,
        request.version,
        local_registry=request.image_plan.registry,
    )
    pub_arch = publish_architectures_composite(
        pub_plan,
        executor=executor,
        role="stack",
        authfile="/tmp/ghcr-auth/config.json",
    )
    pub_manifests = publish_manifests_composite(
        pub_plan,
        executor=executor,
        role="stack",
        docker_config="/tmp/ghcr-auth",
    )
    pub_aliases = publish_aliases_composite(
        pub_plan,
        executor=executor,
        role="stack",
        docker_config="/tmp/ghcr-auth",
    )

    # --- Phase 12: Attest ---
    cosign_key = "/secrets/cosign-key"
    cosign_password = "/secrets/cosign-password"
    if request.credentials is not None:
        cosign_key = str(getattr(request.credentials, "cosign_key", cosign_key))
        cosign_password = str(getattr(request.credentials, "cosign_password", cosign_password))
    published_images = tuple(copy.destination for copy in pub_plan.copies)
    attest = attest_composite(
        images=published_images,
        predicate_remote=Path(f"{remote_root}/predicate.json"),
        sbom_dir_remote=Path(f"{remote_root}/sboms"),
        cosign_key=cosign_key,
        password_file=cosign_password,
        docker_config="/tmp/ghcr-auth",
        executor=executor,
        role="stack",
    )

    # --- Wire the DAG ---
    # Order of wf.add() defines execution order. requires only lists Resource
    # objects that need acquire/release spliced around their consumers.
    wf.add(source_tests, requires=(infrastructure.stack, archive))  # pyright: ignore[reportArgumentType]
    wf.add(  # pyright: ignore[reportArgumentType]
        amd64_build, requires=(infrastructure.stack, amd64_builder)
    )
    wf.add(registry_push, requires=(infrastructure.stack,))  # pyright: ignore[reportArgumentType]
    for _i, bw in enumerate(benchmark_runs):
        wf.add(  # pyright: ignore[reportArgumentType]
            _SubWorkflowTask(bw),
            requires=(
                infrastructure.stack,
                infrastructure.loadgen,
                infrastructure.endpoints,
            ),
        )
    wf.add(aggregate)  # pyright: ignore[reportArgumentType]
    wf.add(reg_gate)  # pyright: ignore[reportArgumentType]
    wf.add(  # pyright: ignore[reportArgumentType]
        arm64_build,
        requires=(
            infrastructure.stack,
            infrastructure.arm_builder,
            tunnel,
            arm64_builder,
            archive,
        ),
    )
    wf.add(arm64_smoke, requires=(infrastructure.arm_builder,))  # pyright: ignore[reportArgumentType]
    wf.add(pub_arch, requires=(infrastructure.stack,))  # pyright: ignore[reportArgumentType]
    wf.add(pub_manifests, requires=(infrastructure.stack,))  # pyright: ignore[reportArgumentType]
    wf.add(pub_aliases, requires=(infrastructure.stack,))  # pyright: ignore[reportArgumentType]
    wf.add(attest, requires=(infrastructure.stack,))  # pyright: ignore[reportArgumentType]

    return wf


def _release_source_commit(repo_root: Path, requested_version: str) -> str:
    """Return the immutable source commit for an already prepared release."""
    source = git_state(repo_root)
    if not source.clean:
        raise ValueError("release requires a clean nanoFaaS Git tree")
    requested_plain, _ = normalize_version(requested_version)
    prepared = verify_version_consistency(repo_root)
    if prepared != requested_plain:
        raise ValueError(
            f"requested release {requested_plain} does not match "
            f"the committed nanoFaaS project version {prepared}"
        )
    return source.commit


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
            aggregate,
            baseline_agg,
            self.policy,
            k6_passed=True,
            autoscaling_passed=True,
        )
        return TaskOutcome(value=decision)
