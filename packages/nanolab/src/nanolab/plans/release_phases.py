"""One function per release phase.

`build_release_workflow` reached 703 lines and 51 live locals, which is more
than a reader can hold at once and is why every change there costs a full
re-read. The phases were already there in the comments; this gives them names.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from sonata_engine import Evidence, Resource, Steps
from sonata_tasks.command import CommandTask
from sonata_tasks.cosign import CosignTask
from nanolab.tasks.release_composites import (
    attest_composite,
    command_specs_composite,
    registry_push_composite,
)
from sonata_tasks.transfer import FileTransferTask
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor

from nanolab.images.plan import ImagePlan
from nanolab.plans import loadtest as loadtest_plan
from nanolab.release.benchmark import (
    _aggregate_from_payload,
    performance_profile,
    regression_policy,
    run_sonata_aggregate,
    run_sonata_benchmark,
    run_sonata_regression_gate,
)
from nanolab.release.model import digest_path
from nanolab.release.build import amd64_build_commands, source_test_commands
from nanolab.release import attest as release_attest
from nanolab.release import build as release_build
from nanolab.release import publish as release_publish
from nanolab.release.metrics import build_release_record
from nanolab.release.model import Amd64ReleasePlan, BuilderConfiguration, ReleaseIdentity

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from nanolab.plans.release import ReleaseRequest
from nanolab.release.resources import (
    ReleaseResources,
    cosign_credentials_resource,
    ghcr_credentials_resource,
    ReleaseSourceResources,
    build_release_source_resources,
)
from nanolab.release.tasks import (
    ReleasePhaseTask,
    aggregate_benchmarks_task,
    amd64_build_task,
    attest_task,
    arm64_build_task,
    arm64_smoke_task,
    benchmark_task,
    regression_gate_task,
    exact_receipt_artifacts,
    finalize_task,
    require_attestation_predicate,
    publish_aliases_task,
    publish_architectures_task,
    publish_manifests_task,
    registry_artifacts_from_receipt,
    registry_evidence,
    require_release_barriers,
    run_steps,
    verified_file_receipt,
    registry_push_task,
    run_image_steps,
    run_source_steps,
    source_test_task,
)


_AGGREGATE_FILENAME = "aggregate.json"


def build_source_test_phase(
    *,
    identity: ReleaseIdentity,
    run_dir: Path,
    release_dir: Path,
    nanofaas: Path,
    source_commit: str,
    remote_root: str,
    source_dir: str,
    provider: Any,
    stack_request: Any,
    arm_request: Any,
    infrastructure: ReleaseResources,
    executor: RoleBoundCommandTaskExecutor,
) -> tuple[ReleaseSourceResources, ReleasePhaseTask]:
    """Stage the release source tree and run the source test suite on it."""
    sources = build_release_source_resources(
        repo_root=nanofaas,
        commit=source_commit,
        run_dir=release_dir,
        remote_source_dir=source_dir,
        remote_archive=f"{remote_root}/source.tar",
        provider=provider,
        stack_request=stack_request,
        arm_request=arm_request,
        stack_requires=(infrastructure.stack,),
        arm_requires=(infrastructure.arm_builder,),
    )
    source_commands = source_test_commands(Path(source_dir))
    source_steps = command_specs_composite(
        source_commands, executor=executor, title="Run source tests"
    )
    source_tests = source_test_task(
        identity=identity,
        run_dir=run_dir,
        phase_inputs={
            "commands": tuple(
                (
                    command.argv,
                    tuple(sorted(command.options.env.items())),
                    str(command.options.remote_dir),
                    str(command.options.cwd),
                    command.options.timeout_seconds,
                )
                for command in source_commands
            )
        },
        work=lambda inputs: run_source_steps(
            source_steps, inputs, source_archive=release_dir / "source.tar"
        ),
    )
    return sources, source_tests


def build_amd64_phase(
    *,
    identity: ReleaseIdentity,
    run_dir: Path,
    image_plan: ImagePlan,
    max_parallelism: int,
    builder_name: str,
    remote_root: str,
    source_dir: str,
    executor: RoleBoundCommandTaskExecutor,
    source_tests: ReleasePhaseTask,
) -> tuple[tuple[str, ...], ReleasePhaseTask]:
    """Build the AMD64 images on the stack VM from the staged source tree."""
    amd64_commands = amd64_build_commands(
        image_plan,
        builder_name=builder_name,
        remote_bake_file=f"{remote_root}/docker-bake-amd64.json",
        remote_source_dir=source_dir,
    )
    amd64_steps = command_specs_composite(
        amd64_commands, executor=executor, title="Build AMD64 images"
    )
    release_images = tuple(cell.image for cell in image_plan.cells)
    amd64_build = amd64_build_task(
        identity=identity,
        run_dir=run_dir,
        phase_inputs={
            "commands": tuple(
                (command.argv, command.role, str(command.options.remote_dir))
                for command in amd64_commands
            ),
            "maxParallelism": max_parallelism,
            "sourceDir": source_dir,
        },
        prerequisites=(source_tests.receipt,),
        expected_images=release_images,
        work=lambda inputs: run_image_steps(
            amd64_steps,
            inputs,
            executor,
            release_images,
            registry=False,
            architecture="amd64",
        ),
    )
    return release_images, amd64_build


def build_registry_push_phase(
    *,
    identity: ReleaseIdentity,
    run_dir: Path,
    image_plan: ImagePlan,
    release_images: tuple[str, ...],
    executor: RoleBoundCommandTaskExecutor,
    source_tests: ReleasePhaseTask,
    amd64_build: ReleasePhaseTask,
) -> ReleasePhaseTask:
    """Push the built AMD64 images into the registry on the stack VM."""
    registry_steps = registry_push_composite(
        image_plan,
        executor=executor,
        role="stack",
        tls_verify=False,
    )
    return registry_push_task(
        identity=identity,
        run_dir=run_dir,
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


def build_benchmark_phase(
    *,
    identity: ReleaseIdentity,
    run_dir: Path,
    benchmark_plan: ReleaseRequest,
    runs: int,
    scenario: Path,
    release_images: tuple[str, ...],
    registry_push: ReleasePhaseTask,
    bindings: Any,
    fetcher: Any,
    endpoints: Any,
) -> tuple[ReleasePhaseTask, ...]:
    """Run the loadtest benchmark the configured number of times."""
    benchmark_runs = []
    for i in range(1, runs + 1):
        benchmark_runs.append(
            benchmark_task(
                index=i,
                identity=identity,
                run_dir=run_dir,
                phase_inputs={
                    "run": i,
                    "scenario": digest_path(scenario),
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
                        inputs.resource(endpoints),
                        registry_push.receipt,
                    ),
                ),
            )
        )
    return tuple(benchmark_runs)


def build_regression_phase(
    *,
    identity: ReleaseIdentity,
    run_dir: Path,
    benchmark_plan: ReleaseRequest,
    runs: int,
    benchmark_runs: tuple[ReleasePhaseTask, ...],
) -> tuple[ReleasePhaseTask, ReleasePhaseTask]:
    """Aggregate the benchmark runs and gate the release on the regression policy."""
    aggregate = aggregate_benchmarks_task(
        identity=identity,
        run_dir=run_dir,
        phase_inputs={
            "runs": runs,
            "profile": asdict(performance_profile(benchmark_plan)),
        },
        prerequisites=tuple(task.receipt for task in benchmark_runs),
        work=lambda _inputs: (
            run_sonata_aggregate(
                benchmark_plan, tuple(task.receipt for task in benchmark_runs)
            ),
        ),
    )
    reg_gate = regression_gate_task(
        identity=identity,
        run_dir=run_dir,
        phase_inputs={"policy": asdict(regression_policy(benchmark_plan))},
        prerequisites=(aggregate.receipt,),
        work=lambda _inputs: (
            run_sonata_regression_gate(benchmark_plan, aggregate.receipt),
        ),
    )
    return aggregate, reg_gate


def build_arm64_phase(
    *,
    request: ReleaseRequest,
    identity: ReleaseIdentity,
    release_dir: Path,
    nanofaas: Path,
    arm_plan: ImagePlan,
    remote_root: str,
    source_dir: str,
    provider: Any,
    arm_request: Any,
    reg_gate: ReleasePhaseTask,
    source_tests: ReleasePhaseTask,
) -> tuple[Amd64ReleasePlan, tuple[str, ...], ReleasePhaseTask, ReleasePhaseTask]:
    """Build the ARM64 images on the ARM builder VM and smoke-test them."""
    arm_runtime_plan = Amd64ReleasePlan(
        repo_root=nanofaas,
        run_dir=release_dir / "domain",
        version=identity.prepared_version,
        identity=identity,
        environment=request.environment,
        scenario=request.scenario,
        settings=request.settings,
        image_plan=arm_plan,
        builder=BuilderConfiguration(
            name=f"release-arm64-{request.version}",
            max_parallelism=request.settings.max_parallelism,
        ),
        bake_file=release_dir / "docker-bake-arm64.json",
        buildkit_config=release_dir / "buildkitd-arm64.toml",
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
                arm_request,
                f"{remote_root}/docker-bake-arm64.json",
                f"{remote_root}/buildkitd-arm64.toml",
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
                arm_request,
                registry_artifacts_from_receipt(arm64_build.receipt, arm_images),
                registry_upstream="",
                ensure_tunnel=False,
            )
        ),
    )
    return arm_runtime_plan, arm_images, arm64_build, arm64_smoke


class PublicationPhase(NamedTuple):
    """What the publication phase hands to the DAG and to attestation."""

    ghcr: Resource[Any] | None
    cosign: Resource[Any] | None
    publish_architectures: ReleasePhaseTask
    publish_manifests: ReleasePhaseTask
    publish_aliases: ReleasePhaseTask
    publication_receipts: tuple[tuple[Path, str], ...]
    all_published: Callable[[], dict[str, str]]
    docker_credentials: Callable[[Any], Any]


def build_publication_phase(
    *,  # NOSONAR (S107): keyword-only inputs mix request, identity and prior-phase results
    request: ReleaseRequest,
    identity: ReleaseIdentity,
    release_dir: Path,
    provider: Any,
    stack_request: Any,
    infrastructure: ReleaseResources,
    release_images: tuple[str, ...],
    registry_push: ReleasePhaseTask,
    reg_gate: ReleasePhaseTask,
    arm64_build: ReleasePhaseTask,
    arm64_smoke: ReleasePhaseTask,
    arm_images: tuple[str, ...],
    arm_runtime_plan: Amd64ReleasePlan,
    pub_plan: Any,
) -> PublicationPhase:
    """Publish the verified images, their manifests and their aliases to GHCR."""
    credentials = request.credentials
    ghcr = (
        ghcr_credentials_resource(
            provider=provider,
            request=stack_request,
            username=release_publish.ghcr_username(pub_plan.repository),
            token_file=credentials.ghcr_token,
            requires=(infrastructure.stack,),
        )
        if credentials is not None
        else None
    )
    cosign = (
        cosign_credentials_resource(
            provider=provider,
            request=stack_request,
            key_file=credentials.cosign_key,
            password_file=credentials.cosign_password,
            requires=(infrastructure.stack,),
        )
        if credentials is not None and credentials.cosign_password is not None
        else None
    )

    def docker_credentials(inputs: Any):
        if ghcr is None:
            raise ValueError("release credential config is required for publication")
        return inputs.resource(ghcr).value

    architecture_references = tuple(f"docker://{copy.destination}" for copy in pub_plan.copies)
    manifest_references = tuple(f"docker://{item.reference}" for item in pub_plan.manifests)
    alias_references = tuple(f"docker://{item.reference}" for item in pub_plan.aliases)

    def published(
        receipt: Path, phase: str, references: tuple[str, ...]
    ) -> dict[str, str]:
        return {
            artifact.reference.removeprefix("docker://"): artifact.digest
            for artifact in exact_receipt_artifacts(
                receipt, phase, "ghcr-digest", references
            )
        }

    def all_published() -> dict[str, str]:
        return {
            **published(
                publish_architectures.receipt,
                "publish-architectures",
                architecture_references,
            ),
            **published(
                publish_manifests.receipt,
                "publish-manifests",
                manifest_references,
            ),
            **published(
                publish_aliases.receipt, "publish-aliases", alias_references
            ),
        }

    def ghcr_evidence(artifacts: tuple[Any, ...]) -> tuple[Evidence, ...]:
        return tuple(
            Evidence("ghcr-digest", artifact.reference, artifact.digest)
            for artifact in artifacts
        )

    def publication_sources():
        arm_build_evidence = require_release_barriers(
            gate_receipt=reg_gate.receipt,
            gate_file=release_dir / "regression-decision.json",
            smoke_receipt=arm64_smoke.receipt,
            smoke_file=arm_runtime_plan.run_dir / "arm64-smoke.json",
            arm_build_receipt=arm64_build.receipt,
            arm_images=arm_images,
        )

        amd64_evidence = exact_receipt_artifacts(
            registry_push.receipt,
            "local-registry-push",
            "local-registry-digest",
            tuple(f"docker://{image}" for image in release_images),
        )
        return release_publish.require_publication_evidence(
            pub_plan, amd64_evidence + arm_build_evidence
        )

    publish_architectures = publish_architectures_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={"plan": pub_plan},
        prerequisites=(
            reg_gate.receipt,
            arm64_smoke.receipt,
            registry_push.receipt,
            arm64_build.receipt,
        ),
        work=lambda inputs: ghcr_evidence(
            release_publish.publish_architecture_images(
                provider,
                stack_request,
                pub_plan,
                publication_sources(),
                authfile=f"{docker_credentials(inputs).docker_config}/config.json",
            )
        ),
    )
    publish_manifests = publish_manifests_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={"manifests": pub_plan.manifests},
        prerequisites=(publish_architectures.receipt,),
        work=lambda inputs: ghcr_evidence(
            release_publish.publish_manifests(
                provider,
                stack_request,
                pub_plan,
                published(
                    publish_architectures.receipt,
                    "publish-architectures",
                    architecture_references,
                ),
                docker_config=docker_credentials(inputs).docker_config,
            )
        ),
    )
    publish_aliases = publish_aliases_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={"aliases": pub_plan.aliases},
        prerequisites=(publish_manifests.receipt,),
        work=lambda inputs: ghcr_evidence(
            release_publish.publish_aliases(
                provider,
                stack_request,
                pub_plan,
                published(
                    publish_manifests.receipt,
                    "publish-manifests",
                    manifest_references,
                ),
                docker_config=docker_credentials(inputs).docker_config,
            )
        ),
    )

    publication_receipts = (
        (publish_architectures.receipt, "publish-architectures"),
        (publish_manifests.receipt, "publish-manifests"),
        (publish_aliases.receipt, "publish-aliases"),
    )
    return PublicationPhase(
        ghcr=ghcr,
        cosign=cosign,
        publish_architectures=publish_architectures,
        publish_manifests=publish_manifests,
        publish_aliases=publish_aliases,
        publication_receipts=publication_receipts,
        all_published=all_published,
        docker_credentials=docker_credentials,
    )


def build_attestation_phase(
    *,  # NOSONAR (S107): keyword-only inputs mix request, identity and prior-phase results
    request: ReleaseRequest,
    identity: ReleaseIdentity,
    release_dir: Path,
    remote_root: str,
    provider: Any,
    stack_request: Any,
    executor: RoleBoundCommandTaskExecutor,
    benchmark_plan: ReleaseRequest,
    aggregate: ReleasePhaseTask,
    cosign: Resource[Any] | None,
    pub_plan: Any,
    publication_receipts: tuple[tuple[Path, str], ...],
    all_published: Callable[[], dict[str, str]],
    docker_credentials: Callable[[Any], Any],
) -> tuple[ReleasePhaseTask, ReleasePhaseTask]:
    """Sign and attest the published digests, then write the release record."""
    def release_record() -> dict[str, Any]:
        aggregate_file = release_dir / _AGGREGATE_FILENAME
        verified_file_receipt(aggregate.receipt, "aggregate", aggregate_file)
        return build_release_record(
            version=request.version,
            source_commit=identity.source_commit,
            image_digests=all_published(),
            aggregate=_aggregate_from_payload(json.loads(aggregate_file.read_text(encoding="utf-8"))),
            policy=regression_policy(benchmark_plan),
        )

    predicate_file = release_dir / "predicate.json"
    remote_predicate = f"{remote_root}/predicate.json"
    remote_sboms = f"{remote_root}/sboms"
    remote_public_key = f"{remote_sboms}/cosign.pub"

    def _pinned(images: Mapping[str, str]) -> tuple[str, ...]:
        """Collapse tags and aliases onto the unique set of pinned digests.

        Aliases point at the same digest as their native manifest, so signing
        by reference would sign the same artifact several times.
        """
        pinned: dict[str, None] = {}
        for reference, digest in sorted(images.items()):
            pinned.setdefault(f"{reference.rsplit(':', 1)[0]}@{digest}", None)
        return tuple(pinned)

    def attest_images(inputs: Any) -> tuple[Evidence, ...]:
        if cosign is None:
            raise ValueError("release Cosign credentials are required for attestation")
        images = all_published()
        aggregate_evidence = verified_file_receipt(
            aggregate.receipt, "aggregate", release_dir / _AGGREGATE_FILENAME
        )
        predicate_file.write_text(
            release_attest.render_predicate(
                release_attest.build_release_predicate(
                    version=request.version,
                    source_commit=identity.source_commit,
                    azure_profile=request.settings.profile,
                    benchmark_record_digest=aggregate_evidence.digest,
                    image_digests=images,
                )
            ),
            encoding="utf-8",
        )
        credentials = inputs.resource(cosign).value
        if credentials.password_file is None:
            raise ValueError("cosign attestation requires a staged password file")
        docker_config = docker_credentials(inputs).docker_config

        # One-shot setup: the SBOM directory (whose parent is the release root
        # the predicate lands in), the predicate itself, and the public half of
        # the signing key -- `cosign verify` rejects the encrypted private key.
        # None of it is per-image, so none of it belongs in the composite.
        # ponytail: re-runs on resume; four cheap calls against six per digest.
        prelude = (
            CommandTask(
                title="Create remote SBOM directory",
                argv=("mkdir", "-p", remote_sboms),
                executor=executor,
                role="stack",
            ),
            FileTransferTask(
                provider=provider,
                request=stack_request,
                source=predicate_file,
                destination=remote_predicate,
                title="Transfer release predicate",
            ),
            CosignTask(
                operation="public-key",
                image="",
                key_file=credentials.key_file,
                password_file=str(credentials.password_file),
                docker_config=docker_config,
                output_file=remote_public_key,
                executor=executor,
                role="stack",
            ),
            # cosign public-key redirects to a file, so a failure that
            # still exits 0 leaves an empty key that makes every later
            # `verify` fail for the wrong reason. Check what landed.
            CommandTask(
                title="Verify derived cosign public key",
                argv=("grep", "-q", "PUBLIC KEY", remote_public_key),
                executor=executor,
                role="stack",
            ),
        )
        for step in prelude:
            # All four overwrite rather than append, so re-entering one is safe
            # -- and a `failed` record on a non-idempotent step makes the next
            # `--resume` raise instead of retrying it. The grep guard exists
            # because `cosign public-key` can fail while exiting 0, so this is
            # a path with a known failure mode, not a theoretical one.
            step.idempotent = True
        run_steps(Steps(title="Stage attestation inputs", steps=prelude), inputs)

        pinned = _pinned(images)
        signed: list[Evidence] = []
        run_steps(
            attest_composite(
                pinned,
                signed=signed,
                predicate_remote=remote_predicate,
                sbom_dir_remote=remote_sboms,
                public_key_remote=remote_public_key,
                cosign_key=credentials.key_file,
                password_file=str(credentials.password_file),
                docker_config=docker_config,
                executor=executor,
                role="stack",
            ),
            inputs,
        )

        # One entry per digest this run signed, emitted by the group that did
        # the signing -- not synthesized from `pinned`, which is a list of work
        # to do rather than work that happened.
        # ponytail: a group the journal skipped appends nothing, so a resumed
        # phase's receipt claims only what it re-signed. That under-claims and
        # never over-claims; carrying a skipped group's evidence forward would
        # need the engine to hand a composite the evidence behind a skip, and
        # `Steps.run` only ever sees `TaskExecution.outcome`, which is None.
        return (
            Evidence("file-digest", str(predicate_file), digest_path(predicate_file)),
            *signed,
        )

    attest = attest_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={"images": tuple(sorted(published_image.destination for published_image in pub_plan.copies))},
        prerequisites=tuple(receipt for receipt, _phase in publication_receipts)
        + (aggregate.receipt,),
        work=attest_images,
    )

    def finalize_documentation(_inputs: Any) -> tuple[Evidence, ...]:
        aggregate_evidence = verified_file_receipt(
            aggregate.receipt, "aggregate", release_dir / _AGGREGATE_FILENAME
        )
        expected_predicate = release_attest.build_release_predicate(
            version=request.version,
            source_commit=identity.source_commit,
            azure_profile=request.settings.profile,
            benchmark_record_digest=aggregate_evidence.digest,
            image_digests=all_published(),
        )
        require_attestation_predicate(attest.receipt, predicate_file, expected_predicate)
        artifacts = release_attest.finalize_release(
            record=release_record(),
            performance_root=request.performance_root,
        )
        return tuple(
            Evidence("file-digest", artifact.reference, artifact.digest) for artifact in artifacts
        )

    finalize = finalize_task(
        identity=identity,
        run_dir=request.run_dir,
        phase_inputs={"performanceRoot": request.performance_root},
        prerequisites=(attest.receipt,) + tuple(
            receipt for receipt, _phase in publication_receipts
        ) + (aggregate.receipt,),
        work=finalize_documentation,
    )
    return attest, finalize
