"""Compile a release scenario into a Sonata workflow.

The 12-phase release pipeline defined in `nanolab release run` replays here as a
linear DAG built from the Task and Resource primitives defined in sonata-tasks.
Each phase that iterates over image cells is a composite Steps node so the
workflow surface is always 12 top-level entries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from sonata_engine import Evidence, JournalConfig, Workflow
from sonata_tasks.buildx import buildx_builder_resource
from sonata_tasks.release_composites import (
    amd64_build_composite,
    attest_composite,
    publish_aliases_composite,
    publish_architectures_composite,
    publish_manifests_composite,
    registry_push_composite,
    source_tests_composite,
)
from nanolab.plans import loadtest as loadtest_plan
from sonata_tasks.registry_tunnel import registry_tunnel_resource
from workflow_tasks.execution.bindings import RoleBoundCommandTaskExecutor

from nanolab.cli.execution import build_role_bindings
from nanolab.cli.vm_provider import vm_provider_for_environment, vm_request_for_role
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.plan import DEFAULT_REGISTRY, ImagePlan, build_image_plan
from nanolab.release.arm import build_arm64_image_plan
from nanolab.release import arm as release_arm
from nanolab.release import build as release_build
from nanolab.release.environment import validate_release_environment
from nanolab.release.benchmark import (
    performance_profile,
    regression_policy,
    run_sonata_aggregate,
    run_sonata_benchmark,
    run_sonata_regression_gate,
)
from nanolab.release.publish import build_publish_plan
from nanolab.release.resources import (
    arm_build_inputs_resource,
    build_release_resources,
    build_release_source_resources,
)
from nanolab.release.run import (
    Amd64ReleasePlan,
    BuilderConfiguration,
    CredentialFiles,
    ReleaseSettings,
    git_state,
    source_test_commands,
)
from nanolab.release.state import ReleaseIdentity, digest_path
from nanolab.release.tasks import (
    aggregate_benchmarks_task,
    amd64_build_task,
    arm64_build_task,
    arm64_smoke_task,
    benchmark_task,
    registry_push_task,
    regression_gate_task,
    run_image_steps,
    run_source_steps,
    registry_artifacts_from_receipt,
    registry_evidence,
    source_test_task,
    versioned_release_run_dir,
)
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


def release_journal_config(request: ReleaseRequest) -> JournalConfig:
    """Use one Sonata journal per prepared release version."""
    return JournalConfig(
        versioned_release_run_dir(request.run_dir, normalize_version(request.version)[0])
        / "sonata.jsonl"
    )


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
    if request.identity is None:
        raise ValueError("release workflow requires a validated release identity")
    identity = request.identity
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
    remote_root = f"/home/azureuser/nanofaas-release/{request.version}"
    source_dir = f"{remote_root}/source"
    release_dir = versioned_release_run_dir(request.run_dir, identity.prepared_version)

    wf = Workflow(workflow_id=f"release-{request.version}")

    # --- Phase 1: Source Tests ---
    sources = build_release_source_resources(
        repo_root=nanofaas,
        commit=source_commit,
        run_dir=release_dir,
        remote_source_dir=source_dir,
        remote_archive=f"{remote_root}/source.tar",
        provider=provider,
        stack_request=stack_req,
        arm_request=arm_req,
        stack_requires=(infrastructure.stack,),
        arm_requires=(infrastructure.arm_builder,),
    )
    source_commands = source_test_commands(Path(source_dir))
    source_steps = source_tests_composite(source_commands, executor=executor)
    source_tests = source_test_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={
            "commands": tuple(
                (
                    command.argv,
                    tuple(sorted(command.env.items())),
                    str(command.remote_dir),
                    str(command.cwd),
                    command.timeout_seconds,
                )
                for command in source_commands
            )
        },
        work=lambda inputs: run_source_steps(source_steps, inputs),
    )

    # --- Phase 2: AMD64 Build ---
    amd64_builder = buildx_builder_resource(
        name=f"release-amd64-{request.version}",
        executor=executor,
        role="stack",
        requires=(infrastructure.stack,),
    )
    amd64_steps = amd64_build_composite(
        request.image_plan,
        executor=executor,
        role="stack",
        cwd=Path(source_dir),
    )
    amd64_build = amd64_build_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={
            "cells": tuple(
                (
                    cell.image,
                    cell.architecture,
                    cell.flavor,
                    cell.build_kind,
                    str(cell.target.dockerfile),
                    str(cell.target.context),
                    cell.gradle_command,
                )
                for cell in request.image_plan.cells
            ),
            "maxParallelism": request.settings.max_parallelism,
            "sourceDir": source_dir,
        },
        prerequisites=(source_tests.receipt,),
        work=lambda inputs: run_image_steps(
            amd64_steps,
            inputs,
            executor,
            tuple(cell.image for cell in request.image_plan.cells),
            registry=False,
        ),
    )

    # --- Phase 3: Registry Push ---
    registry_steps = registry_push_composite(
        request.image_plan,
        executor=executor,
        role="stack",
        tls_verify=False,
    )
    release_images = tuple(cell.image for cell in request.image_plan.cells)
    registry_push = registry_push_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={"images": release_images, "tlsVerify": False},
        prerequisites=(source_tests.receipt, amd64_build.receipt),
        expected_images=release_images,
        work=lambda inputs: run_image_steps(
            registry_steps,
            inputs,
            executor,
            release_images,
            registry=True,
        ),
    )

    # --- Phases 4-6: Benchmarks ---
    benchmark_scenario = _read_yaml(request.settings.scenario)
    benchmark_config = ScenarioConfig.model_validate(benchmark_scenario)
    benchmark_plan = replace(
        request,
        repo_root=Path(source_dir),
        scenario=benchmark_config,
        run_dir=versioned_release_run_dir(request.run_dir, identity.prepared_version),
    )
    benchmark_runs = []
    for i in range(1, request.settings.benchmark_runs + 1):
        benchmark_runs.append(
            benchmark_task(
                index=i,
                identity=identity,
                run_dir=request.run_dir,
                phase_inputs={
                    "run": i,
                    "scenario": digest_path(request.settings.scenario),
                    "images": release_images,
                },
                prerequisites=(registry_push.receipt,),
                work=lambda inputs, index=i: (
                    run_sonata_benchmark(
                        benchmark_plan,
                        index,
                        loadtest_plan.build_loadtest_plan,
                        bindings,
                        fetcher,
                        inputs.resource(infrastructure.endpoints),
                        registry_push.receipt,
                    ),
                ),
            )
        )

    # --- Phase 7: Aggregate ---
    aggregate = aggregate_benchmarks_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={
            "runs": request.settings.benchmark_runs,
            "profile": asdict(performance_profile(benchmark_plan)),
        },
        prerequisites=tuple(task.receipt for task in benchmark_runs),
        work=lambda _inputs: (
            run_sonata_aggregate(
                benchmark_plan, tuple(task.receipt for task in benchmark_runs)
            ),
        ),
    )

    # --- Phase 8: Regression Gate ---
    reg_gate = regression_gate_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={"policy": asdict(regression_policy(benchmark_plan))},
        prerequisites=(aggregate.receipt,),
        work=lambda _inputs: (
            run_sonata_regression_gate(benchmark_plan, aggregate.receipt),
        ),
    )

    # --- Phase 9: ARM64 Build ---
    arm_plan = build_arm64_image_plan(
        nanofaas,
        request.version,
        registry=request.image_plan.registry,
    )
    arm_inputs = arm_build_inputs_resource(
        image_plan=arm_plan,
        max_parallelism=request.settings.max_parallelism,
        run_dir=release_dir,
        remote_root=remote_root,
        provider=provider,
        request=arm_req,
        requires=(infrastructure.arm_builder,),
    )
    tunnel = registry_tunnel_resource(
        registry_upstream=lambda: provider.connection_host(stack_req),
        provider=provider,
        request=arm_req,
        requires=(infrastructure.stack, infrastructure.arm_builder),
    )
    arm64_builder = buildx_builder_resource(
        name=f"release-arm64-{request.version}",
        executor=executor,
        role="arm-builder",
        requires=(infrastructure.arm_builder, arm_inputs),
        buildkitd_config=f"{remote_root}/buildkitd.toml",
        validate=release_arm.require_arm64_builder,
        replace_existing=True,
    )
    arm_runtime_plan = Amd64ReleasePlan(
        repo_root=nanofaas,
        run_dir=release_dir / "domain",
        version=identity.prepared_version,
        identity=identity,
        environment=env,
        scenario=request.scenario,
        settings=request.settings,
        image_plan=request.image_plan,
        builder=BuilderConfiguration(
            name=f"release-arm64-{request.version}",
            max_parallelism=request.settings.max_parallelism,
        ),
        bake_file=release_dir / "docker-bake-arm64.json",
        buildkit_config=release_dir / "buildkitd.toml",
        performance_root=request.performance_root,
        credentials=request.credentials,
    )
    arm_images = tuple(cell.image for cell in arm_plan.cells)
    arm64_build = arm64_build_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={"images": arm_images, "source": identity.source_commit},
        prerequisites=(reg_gate.receipt, source_tests.receipt),
        expected_images=arm_images,
        work=lambda _inputs: registry_evidence(
            release_build._build_arm64_images(  # noqa: SLF001
                arm_runtime_plan,
                arm_plan,
                arm_runtime_plan.bake_file,
                provider,
                arm_req,
                f"{remote_root}/docker-bake-arm64.json",
                f"{remote_root}/buildkitd.toml",
                source_dir,
                registry_upstream="",
                stage_inputs=False,
                manage_resources=False,
            )
        ),
    )

    # --- Phase 10: ARM64 Smoke ---
    arm64_smoke = arm64_smoke_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={"images": arm_images},
        prerequisites=(arm64_build.receipt,),
        work=lambda _inputs: tuple(
            Evidence("file-digest", artifact.reference, artifact.digest)
            for artifact in release_build._smoke_arm64_images(  # noqa: SLF001
                arm_runtime_plan,
                arm_plan,
                provider,
                arm_req,
                registry_artifacts_from_receipt(arm64_build.receipt, arm_images),
                registry_upstream="",
                ensure_tunnel=False,
            )
        ),
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
    wf.add(  # pyright: ignore[reportArgumentType]
        source_tests,
        requires=(infrastructure.stack, sources.stack),
    )
    wf.add(  # pyright: ignore[reportArgumentType]
        amd64_build,
        requires=(infrastructure.stack, sources.stack, amd64_builder),
    )
    wf.add(registry_push, requires=(infrastructure.stack,))  # pyright: ignore[reportArgumentType]
    for benchmark in benchmark_runs:
        wf.add(  # pyright: ignore[reportArgumentType]
            benchmark,
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
            sources.arm,
            arm_inputs,
        ),
    )
    wf.add(  # pyright: ignore[reportArgumentType]
        arm64_smoke,
        requires=(infrastructure.stack, infrastructure.arm_builder, tunnel),
    )
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
