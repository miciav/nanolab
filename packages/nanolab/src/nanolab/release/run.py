"""Azure release planning and execution through the ARM64 functional gate."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
import hashlib
import json
import math
import os
import shlex
import sys
from pathlib import Path
import tempfile
import time
from typing import Any

import yaml

from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.config.environment import ExecutionRole
from nanolab.cli.execution import build_role_bindings
from nanolab.cli.provisioning import provision_environment
from nanolab.cli.vm_provider import (
    vm_provider_for_environment,
    vm_request_for_role,
)
from nanolab.images.bake import render_bake_json
from nanolab.images.plan import DEFAULT_REGISTRY, build_image_plan
from nanolab.plans.loadtest import build_loadtest_plan
from nanolab.release import arm
from nanolab.release.build import (  # noqa: F401 - compatibility re-exports
    _build_amd64_images,
    _build_arm64_images,
    _evidence_map,
    _inspect_ghcr_digest,
    _inspect_image_digest,
    _inspect_registry_digest,
    _local_image_evidence,
    _pinned_image,
    _push_local_images,
    _registry_digest_map,
    _registry_image_evidence,
    _remote_image_digest,
    _require_image_architecture,
    _reset_named_builder,
    _run_source_tests,
    _smoke_arm64_images,
    _smoke_arm64_server,
    _verify_generated_build_inputs,
    amd64_build_commands,
    create_source_archive,
    source_test_commands,
    stage_source_archive,
)
from nanolab.release.benchmark import (  # noqa: F401 - compatibility re-exports
    _evaluate_gate,
    _aggregate_from_payload,
    _aggregate_from_record,
    _decision_from_payload,
    _function_target_name,
    _native_image,
    _performance_profile,
    _pinned_native_image,
    _read_json_file,
    _regression_policy,
    _release_records,
    _run_benchmark,
    _write_aggregate,
)
from nanolab.release.environment import (
    release_lock_path,
    release_run_lock,
    secure_release_endpoints,
    validate_release_environment,
    verify_release_vm_facts,
)
from nanolab.release.metrics import (
    RegressionDecision,
    build_release_record,
)
from nanolab.release import attest, publish
from nanolab.release.secrets import (
    stage_cosign_credentials,
    stage_ghcr_credentials,
)
from nanolab.release.model import (
    Amd64ReleasePlan,
    BuilderConfiguration,
    CredentialFiles,
    GitState,  # noqa: F401 - re-exported for the legacy runner surface
    ReleaseIdentity,
    ReleaseSettings,
    digest_path,
    git_state,
)
from nanolab.release.state import ArtifactEvidence, ReleaseJournal
from nanolab.release.versioning import normalize_version
from workflow_tasks.infra.ansible import AnsibleAdapter
from workflow_tasks.vm.models import VmRequest, vm_remote_home

AMD64_PHASES = (
    "source-tests",
    "amd64-build",
    "local-registry-push",
    "benchmark-1",
    "benchmark-2",
    "benchmark-3",
    "aggregate",
    "regression-gate",
)
RELEASE_PHASES = AMD64_PHASES + arm.ARM64_PHASES + publish.PUBLISH_PHASES + attest.ATTEST_PHASES

def state_directory(plan: Amd64ReleasePlan) -> Path:
    """Where the procedural journal keeps its entries for this release."""
    return plan.journal_root / "releases" / plan.version / "state"


def render_plan(plan: Amd64ReleasePlan) -> str:
    lines = [
        f"release {plan.version} ({plan.identity.source_commit})",
        "provider azure: stack + loadgen",
        (
            f"buildx {plan.builder.name}: docker-container, "
            f"maxParallelism={plan.builder.max_parallelism}"
        ),
        f"images: {len(plan.image_plan.cells)} AMD64 + dynamic ARM64 via Bake + native",
        "ARM64: digest-pinned QEMU after regression-gate",
        (
            "credentials: 3 private files validated; transfer deferred"
            if plan.credentials is not None
            else "credentials: deferred for offline plan"
        ),
    ]
    lines.extend(f"{index:02d}  {phase}" for index, phase in enumerate(RELEASE_PHASES, 1))
    return "\n".join(lines) + "\n"


def build_amd64_release_plan(
        *,
        repo_root: Path,
        version: str,
        environment_path: Path,
        release_config_path: Path,
        run_dir: Path,
        credentials: CredentialFiles | None,
        performance_root: Path | None = None,
) -> Amd64ReleasePlan:
    root = Path(repo_root).resolve()
    environment_file = Path(environment_path).resolve()
    release_config_file = Path(release_config_path).resolve()
    source = git_state(root)
    if not source.clean:
        raise ValueError("release requires a clean Git tree")
    if credentials is not None:
        credentials.validate(repo_root=root)
    plain_version, version_tag = normalize_version(version)
    environment = EnvironmentConfig.model_validate(_read_yaml(environment_file))
    validate_release_environment(environment, root, plain_version)
    settings = _release_settings(root, release_config_file)
    scenario = ScenarioConfig.model_validate(_read_yaml(settings.scenario))
    image_plan = build_image_plan(
        root,
        version_tag,
        registry=DEFAULT_REGISTRY,
        architectures=("amd64",),
    )
    destination = Path(run_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    bake_file = destination / "docker-bake.json"
    bake_file.write_text(render_bake_json(image_plan), encoding="utf-8")
    buildkit_config = destination / "buildkitd.toml"
    buildkit_config.write_text(
        f"[worker.oci]\n  max-parallelism = {settings.max_parallelism}\n",
        encoding="utf-8",
    )
    identity = ReleaseIdentity(
        source_commit=source.commit,
        prepared_version=plain_version,
        release_config_digest=digest_path(release_config_file),
        environment_digest=digest_path(environment_file),
    )
    return Amd64ReleasePlan(
        repo_root=root,
        run_dir=destination,
        version=plain_version,
        identity=identity,
        environment=environment,
        scenario=scenario,
        settings=settings,
        image_plan=image_plan,
        builder=BuilderConfiguration(
            name=f"nanofaas-release-{version_tag.replace('.', '-')}",
            max_parallelism=settings.max_parallelism,
        ),
        bake_file=bake_file,
        buildkit_config=buildkit_config,
        performance_root=(
            Path(performance_root).resolve()
            if performance_root is not None
            else attest.performance_root(root)
        ),
        credentials=credentials,
    )


ProviderFactory = Callable[[EnvironmentConfig, Path], object]
Provisioner = Callable[..., AbstractContextManager[None]]
BuilderProvisioner = Callable[[object, object, Path], None]
LoadtestBuilder = Callable[..., object]
ArchiveBuilder = Callable[[Path, str, Path], ArtifactEvidence]
FailureInjector = Callable[[str], None]


def _release_lock_path(plan: Amd64ReleasePlan) -> Path:
    return release_lock_path(plan.environment)


_release_run_lock = release_run_lock


def _assert_guarded_source(plan: Amd64ReleasePlan) -> None:
    source = git_state(plan.repo_root)
    if not source.clean:
        raise ValueError("release requires a clean Git tree")
    if source.commit != plan.identity.source_commit:
        raise ValueError("release source commit changed after planning")


def run_amd64_release(
        plan: Amd64ReleasePlan,
        *,
        resume: bool = False,
        keep: bool = False,
        provider_factory: ProviderFactory | None = None,
        provisioner: Provisioner | None = None,
        builder_provisioner: BuilderProvisioner | None = None,
        loadtest_builder: LoadtestBuilder | None = None,
        archive_builder: ArchiveBuilder | None = None,
        failure_injector: FailureInjector | None = None,
) -> RegressionDecision:
    """Run a release through the ARM64 functional gate, before publication."""
    with _release_run_lock(_release_lock_path(plan)):
        return _run_amd64_release_locked(
            plan,
            resume=resume,
            keep=keep,
            provider_factory=provider_factory,
            provisioner=provisioner,
            builder_provisioner=builder_provisioner,
            loadtest_builder=loadtest_builder,
            archive_builder=archive_builder,
            failure_injector=failure_injector,
        )


def _run_amd64_release_locked(
        plan: Amd64ReleasePlan,
        *,
        resume: bool,
        keep: bool,
        provider_factory: ProviderFactory | None,
        provisioner: Provisioner | None,
        builder_provisioner: BuilderProvisioner | None,
        loadtest_builder: LoadtestBuilder | None,
        archive_builder: ArchiveBuilder | None,
        failure_injector: FailureInjector | None,
) -> RegressionDecision:
    if plan.credentials is None:
        raise ValueError("release run requires explicit credential files")
    global _RUN_STARTED
    _RUN_STARTED = time.monotonic()
    plan.credentials.validate(repo_root=plan.repo_root)
    _assert_guarded_source(plan)
    existing_entries = tuple(state_directory(plan).glob("*.json"))
    if existing_entries and not resume:
        raise ValueError("release journal already exists; pass --resume to verify and reuse it")
    if resume and not existing_entries:
        raise ValueError("--resume requires an existing release journal")

    make_provider = provider_factory or vm_provider_for_environment
    provision = provisioner or provision_environment
    provision_builder = builder_provisioner or _provision_release_builder
    make_loadtest = loadtest_builder or build_loadtest_plan
    make_archive = archive_builder or create_source_archive
    provider = make_provider(plan.environment, plan.repo_root)
    stack_request = vm_request_for_role(plan.environment, "stack", loadtest=True)
    loadgen_request = vm_request_for_role(plan.environment, "loadgen", loadtest=True)
    arm_request = vm_request_for_role(plan.environment, "arm-builder")
    if not resume:
        _progress("→ tearing down any previous release VMs")
        for role, request in (
                ("stack", stack_request),
                ("loadgen", loadgen_request),
                ("arm-builder", arm_request),
        ):
            _require_result(
                provider.teardown(request),  # type: ignore[attr-defined]
                f"recreate dedicated release {role} VM",
            )
    ghcr_auth: dict[str, str] = {}
    journal = ReleaseJournal(
        plan.journal_root,
        plan.identity,
        phases=RELEASE_PHASES,
        artifact_digest=lambda location, reference: _remote_image_digest(
            provider,
            stack_request,
            location,
            reference,
            ghcr_authfile=ghcr_auth.get("authfile"),
        ),
    )
    endpoints: tuple[str, str] | None = None

    def verify_and_secure(role: ExecutionRole, request: VmRequest) -> None:
        nonlocal endpoints
        _verify_release_vm_facts(plan, provider, role, request)
        if role == "stack":
            _secure_release_endpoints(plan, provider, stack_request, None)
        elif role == "arm-builder":
            # The ARM builder pushes through a localhost:5000 tunnel into the
            # stack registry: allow only its address on the registry port.
            arm_host = provider.connection_host(request)  # type: ignore[attr-defined]
            provider.restrict_inbound_sources(  # type: ignore[attr-defined]
                stack_request,
                ports=(5000,),
                source_cidrs=(f"{arm_host}/32",),
                # Separate priority band from the 30080/81/90 endpoint rules
                # (1010-1012) so the registry rule never collides.
                priority_base=1020,
            )
        else:
            endpoints = _secure_release_endpoints(
                plan, provider, stack_request, loadgen_request
            )

    _progress("→ provisioning stack + loadgen VMs (this can take several minutes)")
    with provision(
            plan.scenario,
            plan.environment,
            repo_root=plan.repo_root,
            orchestrator_factory=lambda _root: provider,
            post_ensure_verifier=verify_and_secure,
            keep=keep,
    ):
        if endpoints is None:
            raise RuntimeError("release loadgen ingress was not verified before bootstrap")
        control_plane_url, prometheus_url = endpoints
        _progress("→ provisioning buildx release builder")
        provision_builder(provider, stack_request, plan.repo_root)
        provision_builder(provider, arm_request, plan.repo_root)
        if resume:
            _progress("→ resume: verifying journal evidence against GHCR")
            credentials = plan.credentials
            assert credentials is not None
            # published evidence lives on GHCR: verify it authenticated, then
            # drop the staged token again before any build phase runs
            with stage_ghcr_credentials(
                    provider,
                    stack_request,
                    username=publish.ghcr_username(),
                    token_file=credentials.ghcr_token,
            ) as resume_auth:
                ghcr_auth["authfile"] = f"{resume_auth.docker_config}/config.json"
                try:
                    reusable = frozenset(journal.resume().reusable_phases)
                finally:
                    ghcr_auth.pop("authfile", None)
        else:
            reusable = frozenset()
        if reusable:
            _progress(f"↷ reusing verified phases: {', '.join(p for p in RELEASE_PHASES if p in reusable)}")
        remote_root = f"{vm_remote_home(stack_request)}/nanofaas-release/{plan.version}"
        source_dir = f"{remote_root}/source"
        source_archive = f"{remote_root}/source.tar"
        remote_bake = f"{remote_root}/{plan.bake_file.name}"
        remote_buildkit = f"{remote_root}/{plan.buildkit_config.name}"

        if "source-tests" not in reusable:
            _record_phase(
                journal,
                "source-tests",
                lambda: _run_source_tests(
                    plan,
                    provider,
                    stack_request,
                    make_archive,
                    source_archive,
                    source_dir,
                ),
                failure_injector,
            )
        if "amd64-build" not in reusable:
            local_archive = plan.run_dir / "source.tar"
            archive_evidence = _journal_artifact(
                journal,
                "source-tests",
                location="local",
                reference=str(local_archive),
            )

            def build_amd64() -> tuple[ArtifactEvidence, ...]:
                stage_source_archive(
                    provider,
                    stack_request,
                    archive=local_archive,
                    remote_archive=source_archive,
                    remote_source_dir=source_dir,
                    expected_digest=archive_evidence.digest,
                )
                return _build_amd64_images(
                    plan,
                    provider,
                    stack_request,
                    remote_bake,
                    remote_buildkit,
                    source_dir,
                )

            _record_phase(
                journal,
                "amd64-build",
                build_amd64,
                failure_injector,
            )
        if "local-registry-push" not in reusable:
            build_evidence = _journal_phase_artifacts(journal, "amd64-build")
            registry_evidence = _record_phase(
                journal,
                "local-registry-push",
                lambda: _push_local_images(
                    plan,
                    provider,
                    stack_request,
                    build_evidence,
                ),
                failure_injector,
            )
        else:
            registry_evidence = _journal_phase_artifacts(journal, "local-registry-push")

        benchmark_phases = tuple(
            f"benchmark-{index}" for index in range(1, plan.settings.benchmark_runs + 1)
        )
        pending_benchmarks = tuple(phase for phase in benchmark_phases if phase not in reusable)
        if pending_benchmarks:
            expected_registry_digests = _registry_digest_map(plan.image_plan, registry_evidence)
            bindings, fetcher = build_role_bindings(
                plan.environment,
                vm_provider=provider,
                repo_root=plan.repo_root,
            )
        for index in range(1, plan.settings.benchmark_runs + 1):
            phase = f"benchmark-{index}"
            if phase in reusable:
                continue
            _record_phase(
                journal,
                phase,
                lambda index=index: _run_benchmark(
                    plan,
                    index,
                    make_loadtest,
                    bindings,
                    fetcher,
                    control_plane_url,
                    prometheus_url,
                    provider,
                    stack_request,
                    expected_registry_digests,
                ),
                failure_injector,
            )

        if "aggregate" not in reusable:
            _record_phase(
                journal,
                "aggregate",
                lambda: (_write_aggregate(plan, journal),),
                failure_injector,
            )
        if "regression-gate" not in reusable:
            decision_box: list[RegressionDecision] = []

            def gate() -> tuple[ArtifactEvidence, ...]:
                _assert_guarded_source(plan)
                decision, artifact = _evaluate_gate(plan, journal)
                decision_box.append(decision)
                if not decision.passed:
                    raise RuntimeError(
                        "release regression gate failed: " + "; ".join(decision.failures)
                    )
                return (artifact,)

            _record_phase(journal, "regression-gate", gate, failure_injector)
            decision = decision_box[0]
        else:
            decision = _decision_from_payload(
                _read_verified_local_json(
                    journal,
                    "regression-gate",
                    plan.run_dir / "regression-decision.json",
                )
            )
        if not decision.passed:
            raise RuntimeError("release regression gate evidence did not pass")

        arm_plan = arm.build_arm64_image_plan(
            plan.repo_root,
            plan.version,
            registry=plan.image_plan.registry,
        )
        arm_bake = plan.run_dir / "docker-bake-arm64.json"
        remote_arm_bake = f"{remote_root}/{arm_bake.name}"
        if "arm64-build" not in reusable:
            local_archive = plan.run_dir / "source.tar"
            archive_evidence = _journal_artifact(
                journal,
                "source-tests",
                location="local",
                reference=str(local_archive),
            )

            def build_arm64() -> tuple[ArtifactEvidence, ...]:
                _assert_guarded_source(plan)
                stage_source_archive(
                    provider,
                    arm_request,
                    archive=local_archive,
                    remote_archive=source_archive,
                    remote_source_dir=source_dir,
                    expected_digest=archive_evidence.digest,
                )
                return _build_arm64_images(
                    plan,
                    arm_plan,
                    arm_bake,
                    provider,
                    arm_request,
                    remote_arm_bake,
                    remote_buildkit,
                    source_dir,
                    registry_upstream=provider.connection_host(stack_request),  # type: ignore[attr-defined]
                )

            _record_phase(journal, "arm64-build", build_arm64, failure_injector)
        if "arm64-smoke" not in reusable:
            build_evidence = _journal_phase_artifacts(journal, "arm64-build")
            _record_phase(
                journal,
                "arm64-smoke",
                lambda: _smoke_arm64_images(
                    plan,
                    arm_plan,
                    provider,
                    arm_request,
                    build_evidence,
                    registry_upstream=provider.connection_host(stack_request),  # type: ignore[attr-defined]
                ),
                failure_injector,
            )

        _publish_release(
            plan,
            journal,
            provider,
            stack_request,
            reusable,
            failure_injector,
        )
        _attest_release(
            plan,
            journal,
            provider,
            stack_request,
            remote_root,
            reusable,
            failure_injector,
        )
        return decision


def _publish_release(
        plan: Amd64ReleasePlan,
        journal: ReleaseJournal,
        provider: object,
        stack_request: VmRequest,
        reusable: frozenset[str],
        failure_injector: FailureInjector | None,
) -> None:
    """Promote the verified multi-architecture matrix to GHCR: immutable architecture
    tags first, verified version manifests next, mutable aliases last. The
    GHCR token is transferred only for this window and always cleaned up."""
    pending = tuple(phase for phase in publish.PUBLISH_PHASES if phase not in reusable)
    if not pending:
        return
    assert plan.credentials is not None
    _assert_guarded_source(plan)
    # both gates must hold verified evidence before any publication is planned
    _journal_phase_artifacts(journal, "regression-gate")
    _journal_phase_artifacts(journal, "arm64-smoke")
    publish_plan = publish.build_publish_plan(
        plan.repo_root,
        plan.version,
        local_registry=plan.image_plan.registry,
    )
    source_digests = publish.require_publication_evidence(
        publish_plan,
        _journal_phase_artifacts(journal, "local-registry-push")
        + _journal_phase_artifacts(journal, "arm64-build"),
    )
    with stage_ghcr_credentials(
            provider,
            stack_request,
            username=publish.ghcr_username(),
            token_file=plan.credentials.ghcr_token,
    ) as docker:
        authfile = f"{docker.docker_config}/config.json"
        if "publish-architectures" not in reusable:
            _record_phase(
                journal,
                "publish-architectures",
                lambda: publish.publish_architecture_images(
                    provider,
                    stack_request,
                    publish_plan,
                    source_digests,
                    authfile=authfile,
                ),
                failure_injector,
            )
        if "publish-manifests" not in reusable:
            architecture_digests = {
                artifact.reference.removeprefix("docker://"): artifact.digest
                for artifact in _journal_phase_artifacts(journal, "publish-architectures")
            }
            manifest_evidence = _record_phase(
                journal,
                "publish-manifests",
                lambda: publish.publish_manifests(
                    provider,
                    stack_request,
                    publish_plan,
                    architecture_digests,
                    docker_config=docker.docker_config,
                ),
                failure_injector,
            )
        else:
            manifest_evidence = _journal_phase_artifacts(journal, "publish-manifests")
        if "publish-aliases" not in reusable:
            manifest_digests = {
                artifact.reference.removeprefix("docker://"): artifact.digest
                for artifact in manifest_evidence
            }
            _record_phase(
                journal,
                "publish-aliases",
                lambda: publish.publish_aliases(
                    provider,
                    stack_request,
                    publish_plan,
                    manifest_digests,
                    docker_config=docker.docker_config,
                ),
                failure_injector,
            )


def _attest_release(
        plan: Amd64ReleasePlan,
        journal: ReleaseJournal,
        provider: object,
        stack_request: VmRequest,
        remote_root: str,
        reusable: frozenset[str],
        failure_injector: FailureInjector | None,
) -> None:
    """SBOM, sign, and verify the published digests, then finalize records.

    Signing material reaches the VM only for this window. Published
    performance history changes only after verification succeeds; a
    documentation failure appends no final journal record so --resume can
    retry finalization without rebuilding verified images."""
    pending = tuple(phase for phase in attest.ATTEST_PHASES if phase not in reusable)
    if not pending:
        return
    credentials = plan.credentials
    assert credentials is not None
    _assert_guarded_source(plan)
    published = (
            _journal_phase_artifacts(journal, "publish-architectures")
            + _journal_phase_artifacts(journal, "publish-manifests")
            + _journal_phase_artifacts(journal, "publish-aliases")
    )
    images = {
        artifact.reference.removeprefix("docker://"): artifact.digest
        for artifact in published
    }
    benchmark_digest = _journal_phase_artifacts(journal, "aggregate")[0].digest
    record = build_release_record(
        version=plan.version,
        source_commit=plan.identity.source_commit,
        image_digests=images,
        aggregate=_aggregate_from_payload(
            _read_verified_local_json(journal, "aggregate", plan.run_dir / "aggregate.json")
        ),
        policy=_regression_policy(plan),
    )
    if "attest" not in reusable:
        predicate = attest.build_release_predicate(
            version=plan.version,
            source_commit=plan.identity.source_commit,
            azure_profile=plan.settings.profile,
            benchmark_record_digest=benchmark_digest,
            image_digests=images,
        )
        predicate_file = plan.run_dir / "predicate.json"
        predicate_file.write_text(attest.render_predicate(predicate), encoding="utf-8")
        remote_predicate = f"{remote_root}/predicate.json"

        def sign() -> tuple[ArtifactEvidence, ...]:
            _provider_exec(provider, stack_request, ("mkdir", "-p", remote_root))
            _provider_transfer_to(
                provider,
                stack_request,
                source=predicate_file,
                destination=remote_predicate,
                action="transfer release predicate",
            )
            with stage_ghcr_credentials(
                    provider,
                    stack_request,
                    username=publish.ghcr_username(),
                    token_file=credentials.ghcr_token,
            ) as docker, stage_cosign_credentials(
                provider,
                stack_request,
                key_file=credentials.cosign_key,
                password_file=credentials.cosign_password,
            ) as cosign_files:
                attest.attest_release_images(
                    provider,
                    stack_request,
                    images=images,
                    predicate_remote=remote_predicate,
                    sbom_dir_remote=f"{remote_root}/sboms",
                    cosign=cosign_files,
                    docker_config=docker.docker_config,
                )
            return (
                ArtifactEvidence("local", str(predicate_file), digest_path(predicate_file)),
            )

        _record_phase(journal, "attest", sign, failure_injector)
    if "finalize" not in reusable:
        # deliberately outside _record_phase: a documentation failure must
        # leave the journal without any finalize entry, not a failed one
        if failure_injector is not None:
            failure_injector("finalize")
        attest.finalize_release(
            journal,
            record=record,
            performance_root=plan.performance_root,
        )


def _verify_release_vm_facts(
        plan: Amd64ReleasePlan,
        provider: object,
        role: ExecutionRole,
        request: VmRequest,
) -> None:
    verify_release_vm_facts(plan.environment, provider, role, request)


def _secure_release_endpoints(
        plan: Amd64ReleasePlan,
        provider: object,
        stack_request: VmRequest,
        loadgen_request: VmRequest | None,
) -> tuple[str, str]:
    return secure_release_endpoints(
        plan.environment, provider, stack_request, loadgen_request
    )


_RUN_STARTED: float | None = None


def _elapsed_total() -> str:
    if _RUN_STARTED is None:
        return ""
    total = int(time.monotonic() - _RUN_STARTED)
    return f" [tot {total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}]"


def _progress(message: str) -> None:
    print(f"{message}{_elapsed_total()}", file=sys.stderr, flush=True)


def _phase_banner(phase: str) -> str:
    if phase not in RELEASE_PHASES:
        return phase
    index = RELEASE_PHASES.index(phase) + 1
    total = len(RELEASE_PHASES)
    filled = round(20 * index / total)
    return f"[{index:2d}/{total}] {'█' * filled}{'░' * (20 - filled)} {phase}"


def _record_phase(
        journal: ReleaseJournal,
        phase: str,
        action: Callable[[], Iterable[ArtifactEvidence]],
        failure_injector: FailureInjector | None,
) -> tuple[ArtifactEvidence, ...]:
    _progress(f"→ {_phase_banner(phase)} ...")
    started = time.monotonic()
    try:
        if failure_injector is not None:
            failure_injector(phase)
        artifacts = tuple(action())
        if failure_injector is not None:
            failure_injector(f"{phase}:after-action")
    except BaseException:
        journal.record(phase, outcome="failed")
        _progress(f"✗ {phase} failed after {int(time.monotonic() - started)}s")
        raise
    journal.record(phase, artifacts=artifacts)
    _progress(f"✓ {phase} done in {int(time.monotonic() - started)}s")
    return artifacts


def _journal_phase_artifacts(
        journal: ReleaseJournal,
        phase: str,
) -> tuple[ArtifactEvidence, ...]:
    for entry in reversed(journal.entries()):
        if entry.get("kind") != "phase" or entry.get("phase") != phase:
            continue
        if entry.get("outcome") != "passed":
            break
        return tuple(
            ArtifactEvidence(
                str(artifact["location"]),
                str(artifact["reference"]),
                str(artifact["digest"]),
            )
            for artifact in entry["artifacts"]
        )
    raise ValueError(f"release journal has no reusable evidence for {phase}")


def _journal_artifact(
        journal: ReleaseJournal,
        phase: str,
        *,
        location: str,
        reference: str,
) -> ArtifactEvidence:
    matches = tuple(
        artifact
        for artifact in _journal_phase_artifacts(journal, phase)
        if artifact.location == location and artifact.reference == reference
    )
    if len(matches) != 1:
        raise RuntimeError(f"{phase} evidence changed before consumption")
    return matches[0]


def _read_verified_local_json(
        journal: ReleaseJournal,
        phase: str,
        path: Path,
) -> dict[str, Any]:
    artifact = _journal_artifact(
        journal,
        phase,
        location="local",
        reference=str(path),
    )
    try:
        content = path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"{phase} evidence changed before consumption") from error
    actual_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if actual_digest != artifact.digest:
        raise RuntimeError(f"{phase} evidence changed before consumption")
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON evidence must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> ArtifactEvidence:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return ArtifactEvidence("local", str(path), digest_path(path))


def _provision_release_builder(provider: object, request: object, repo_root: Path) -> None:
    private_key = provider.ssh_private_key_path(request)  # type: ignore[attr-defined]
    adapter = AnsibleAdapter(
        repo_root,
        host_resolver=lambda target, dry_run=False: provider.connection_host(target),  # type: ignore[attr-defined]
        private_key_path=private_key,
    )
    result = adapter.provision_release_builder(request)  # type: ignore[arg-type]
    _require_result(result, "release builder provisioning")


def _provider_exec(
        provider: object,
        request: object,
        argv: tuple[str, ...],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        bounded: bool = False,
) -> object:
    if bounded:
        # The SSH executor waits for the exit status before draining output,
        # so a command whose output exceeds the channel window (~2MB)
        # deadlocks: the remote writer blocks and the command never exits.
        # Bulk commands (tests, image builds, pushes) buffer output remotely
        # and return only a 64KB tail — plenty for diagnosis, and stderr is
        # folded into stdout. Commands whose stdout gets parsed must NOT be
        # bounded.
        script = shlex.join(argv)
        argv = (
            "sh",
            "-c",
            "{ " + script + " ; } >/tmp/release-cmd.log 2>&1; "
                            "ec=$?; tail -c 65536 /tmp/release-cmd.log; exit $ec",
        )
    result = _retry_on_connection_death(
        lambda: provider.exec_argv(  # type: ignore[attr-defined]
            request, argv, env=env, cwd=cwd, dry_run=False
        ),
        describe="remote command",
    )
    return _require_result(result, "remote release command")


def _provider_transfer_to(
        provider: object,
        request: object,
        *,
        source: Path,
        destination: str,
        action: str,
) -> object:
    result = _retry_on_connection_death(
        lambda: provider.transfer_to(  # type: ignore[attr-defined]
            request, source=source, destination=destination
        ),
        describe=f"transfer {source.name}",
    )
    return _require_result(result, action)


def _retry_on_connection_death(operation: Callable[[], object], *, describe: str) -> object:
    """Run a remote operation, retrying only when the connection dies.

    Every release remote operation is idempotent (tests, digest-pinned
    builds/pushes/transfers, mkdir -p), so a dropped connection mid-operation
    is safe to re-run. A return_code of -1 is paramiko's "channel closed
    without an exit status" sentinel — never a real shell exit code — and a
    raised exception is a connect-time failure; both mean the connection
    failed, not the operation. Real non-zero exits are NOT retried.
    """
    attempts = 4
    for attempt in range(1, attempts + 1):
        last = attempt == attempts
        try:
            result = operation()
        except Exception as error:  # noqa: BLE001 - reconnect on any transport error
            if last:
                raise
            _progress(f"  ⟳ {describe} connection error ({error}); retry {attempt}/{attempts - 1}")
            time.sleep(min(5 * attempt, 30))
            continue
        if int(getattr(result, "return_code", 0)) == -1 and not last:
            _progress(f"  ⟳ {describe} connection dropped; retry {attempt}/{attempts - 1}")
            time.sleep(min(5 * attempt, 30))
            continue
        return result
    raise AssertionError("unreachable")  # pragma: no cover


def _require_result(result: object, action: str) -> object:
    return_code = int(getattr(result, "return_code", 0))
    if return_code != 0:
        detail = str(getattr(result, "stderr", "") or getattr(result, "stdout", ""))
        raise RuntimeError(detail or f"{action} failed (exit {return_code})")
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"configuration must be an object: {path}")
    return value


def _release_settings(_repo_root: Path, path: Path) -> ReleaseSettings:
    data = _read_yaml(path)
    try:
        if data["schemaVersion"] != 1:
            raise ValueError("release configuration schemaVersion must be 1")
        build = data["build"]
        benchmark = data["benchmark"]
        regression = benchmark["regression"]
        max_parallelism = build["maxParallelism"]
        runs = benchmark["runs"]
        if (
                not isinstance(max_parallelism, int)
                or isinstance(max_parallelism, bool)
                or max_parallelism < 1
        ):
            raise ValueError("build.maxParallelism must be a positive integer")
        if not isinstance(runs, int) or isinstance(runs, bool) or runs != 3:
            raise ValueError("benchmark.runs must be the integer 3")
        scenario = Path(str(benchmark["scenario"]))
        config_root = Path(path).parent.resolve()
        scenario_source = (config_root / scenario).resolve()
        try:
            scenario_relative = scenario_source.relative_to(config_root)
        except ValueError:
            raise ValueError("benchmark scenario must be a configuration-relative file") from None
        if scenario.is_absolute() or not scenario_source.is_file():
            raise ValueError("benchmark scenario must be a configuration-relative file")
        throughput_loss = _finite_nonnegative(
            regression["throughputMaxLossPercent"],
            "benchmark.regression.throughputMaxLossPercent",
        )
        p95_increase = _finite_nonnegative(
            regression["p95MaxIncreasePercent"],
            "benchmark.regression.p95MaxIncreasePercent",
        )
        try:
            error_rate = _number(regression["errorRateMax"], "benchmark.regression.errorRateMax")
        except ValueError as error:
            raise ValueError("benchmark.regression.errorRateMax must be between 0 and 1") from error
        if not 0 <= error_rate <= 1:
            raise ValueError("benchmark.regression.errorRateMax must be between 0 and 1")
        return ReleaseSettings(
            max_parallelism=max_parallelism,
            scenario=scenario_source,
            scenario_name=scenario_relative.as_posix(),
            benchmark_runs=runs,
            profile=str(benchmark["profile"]),
            throughput_max_loss_percent=throughput_loss,
            p95_max_increase_percent=p95_increase,
            error_rate_max=error_rate,
        )
    except (KeyError, TypeError) as error:
        raise ValueError("release configuration is incomplete") from error


def _number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _finite_nonnegative(value: object, name: str) -> float:
    try:
        number = _number(value, name)
    except ValueError as error:
        raise ValueError(f"{name} must be finite nonnegative") from error
    if number < 0:
        raise ValueError(f"{name} must be finite nonnegative")
    return number
