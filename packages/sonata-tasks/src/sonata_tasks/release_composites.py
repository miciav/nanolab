"""Composite Steps builders for the nanoFaaS release workflow.

Each function returns a ``Steps`` that groups one phase of the release
pipeline into a single compiled unit, so a workflow can add it as one
entry and the compiler resolves the serial execution of its steps.

Phases that iterate over image cells use nested ``Steps`` — one inner
unit per cell — so the journal replays at cell granularity without
re-running every cell on a partial failure.

Imports from companion tasks
---------------------------
- :mod:`sonata_tasks.docker` :    DockerBuildTask, DockerPushTask
- :mod:`sonata_tasks.skopeo` :    SkopeoCopyTask, SkopeoInspectTask
- :mod:`sonata_tasks.imagetools` : ImagetoolsCreateTask
- :mod:`sonata_tasks.syft` :      SyftTask
- :mod:`sonata_tasks.cosign` :    CosignTask
- :mod:`sonata_tasks.command` :   CommandTask
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from sonata_engine import Evidence, ReusableTask, Steps, Task, TaskInputs, TaskOutcome
from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import CommandTaskSpec

from sonata_tasks.command import CommandTask
from sonata_tasks.cosign import CosignTask
from sonata_tasks.docker import DockerBuildTask, DockerPushTask
from sonata_tasks.gradle import GradleTask
from sonata_tasks.imagetools import ImagetoolsCreateTask
from sonata_tasks.skopeo import SkopeoCopyTask, SkopeoInspectTask
from sonata_tasks.syft import SyftTask

__all__ = [
    "command_specs_composite",
    "registry_push_composite",
    "arm64_build_composite",
    "arm64_smoke_composite",
    "publish_architectures_composite",
    "publish_manifests_composite",
    "publish_aliases_composite",
    "attest_composite",
]


class _PlanItems:
    """Normalised view of a publish plan so composites don't sniff types."""

    def __init__(self, plan: Any) -> None:
        self.copies: tuple[Any, ...] = ()
        self.manifests: tuple[Any, ...] = ()
        self.aliases: tuple[Any, ...] = ()

        if hasattr(plan, "copies"):
            self.copies = tuple(plan.copies)
            self.manifests = tuple(getattr(plan, "manifests", ()))
            self.aliases = tuple(getattr(plan, "aliases", ()))
        elif hasattr(plan, "cells"):
            self.copies = tuple(plan.cells)
            self.manifests = plan.manifests if hasattr(plan, "manifests") else ()
            self.aliases = plan.aliases if hasattr(plan, "aliases") else ()


# ---------------------------------------------------------------------------
# 1. command_specs_composite
# ---------------------------------------------------------------------------


def command_specs_composite(
    commands: Sequence[CommandTaskSpec],
    executor: CommandTaskExecutor,
    *,
    title: str,
) -> Steps:
    """Wrap a list of command specs into a Steps composite.

    Each ``CommandTaskSpec`` becomes a ``CommandTask``, run in order, journalled
    individually so a resumed phase skips the commands it already finished.

    This is the generic spec-to-Steps bridge: the release phases that already
    have a tested command generator (`source_test_commands`,
    `amd64_build_commands`) feed it here rather than rebuilding their argv.

    Parameters
    ----------
    commands :
        One spec per command.  ``spec.summary`` becomes the task title,
        ``spec.argv`` the command line, ``spec.role`` the execution role.
    executor :
        Role-bound executor that runs each command.
    title :
        Title of the composite, and the prefix of every step id in the journal.
    """
    steps = tuple(
        CommandTask(
            title=spec.summary or f"source-test-{i}",
            argv=spec.argv,
            executor=executor,
            role=spec.role or "stack",
            env=spec.env,
            cwd=spec.cwd,
            remote_dir=spec.remote_dir,
            expected_exit_codes=spec.expected_exit_codes,
            timeout_seconds=spec.timeout_seconds,
        )
        for i, spec in enumerate(commands)
    )
    return Steps(title=title, steps=steps)


# ---------------------------------------------------------------------------
# 3. registry_push_composite
# ---------------------------------------------------------------------------


def registry_push_composite(
    plan: Any,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    authfile: str = "",
    *,
    tls_verify: bool = True,
    title: str = "Push images to registry",
) -> Steps:
    """Push each image to the registry, then inspect its digest.

    Produces one inner ``Steps`` per cell containing a
    ``DockerPushTask`` and a ``SkopeoInspectTask``.  The inner
    grouping keeps the push-and-inspect pair replayable at cell
    granularity.

    Parameters
    ----------
    plan :
        ImagePlan-like object with a ``cells`` attribute.
    executor :
        Role-bound executor.
    role :
        Execution role.
    authfile :
        Path to the registry auth file for the skopeo inspect step.
        Pass empty string only when no authentication is needed
        (e.g. local test registries without auth).
    tls_verify :
        Set False for plain-HTTP registries (e.g. localhost:5000).
    title :
        Optional override.
    """
    cell_steps = tuple(
        Steps(
            title=f"Push and inspect {cell.image}",
            steps=(
                DockerPushTask(
                    image=cell.image,
                    executor=executor,
                    role=role,
                ),
                SkopeoInspectTask(
                    reference=cell.image,
                    authfile=authfile,
                    executor=executor,
                    role=role,
                    tls_verify=tls_verify,
                ),
            ),
        )
        for cell in plan.cells
    )
    return Steps(title=title, steps=cell_steps)


# ---------------------------------------------------------------------------
# 4. arm64_build_composite
# ---------------------------------------------------------------------------


def arm64_build_composite(
    plan: Any,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    builder_name: str,
    *,
    title: str = "Build ARM64 images",
    remote_bake_file: str = "",
    remote_source_dir: str = "",
    registry_upstream: str = "",
) -> Steps:
    """Build ARM64 images on a remote buildx builder.

    Uses the builder and registry tunnel owned by workflow resources, then runs
    the bake and each Gradle native build.
    Uses the remote VM's toolchain via the given execution role.

    Parameters
    ----------
    plan :
        ImagePlan-like object.
    executor :
        Role-bound executor (typically for the ``arm-builder`` role).
    role :
        Execution role (typically ``"arm-builder"``).
    builder_name :
        Name of the buildx builder to create on the remote VM.
    title :
        Optional override.
    remote_bake_file :
        Path (on the remote VM) to the bake file.
    remote_source_dir :
        Working directory on the remote VM.
    registry_upstream :
        Host:port of the stack's registry the tunnel will forward.
    """
    # ponytail: one composite per cell would need VM-level orchestration
    # not available here yet; the current shape runs the full bake +
    # all gradle builds as a single batch step.
    steps: list[Any] = []

    arm64_cells = [c for c in plan.cells if c.architecture == "arm64"]
    arm64_bake_cells = [c for c in arm64_cells if c.build_kind == "bake"]
    arm64_gradle_cells = [c for c in arm64_cells if c.build_kind == "gradle"]

    if arm64_bake_cells:
        if remote_bake_file:
            steps.append(
                CommandTask(
                    title="Bake ARM64 Dockerfile images",
                    argv=(
                        "docker",
                        "buildx",
                        "bake",
                        "--builder",
                        builder_name,
                        "--file",
                        remote_bake_file,
                        "--load",
                        "docker-arm64",
                    ),
                    executor=executor,
                    role=role,
                    cwd=Path(remote_source_dir) if remote_source_dir else None,
                )
            )
        else:
            for cell in arm64_bake_cells:
                steps.append(
                    DockerBuildTask(
                        image=cell.image,
                        dockerfile=str(cell.target.dockerfile),
                        context=str(cell.target.context),
                        executor=executor,
                        role=role,
                        title=f"Build ARM64 {cell.image}",
                    )
                )

    for cell in arm64_gradle_cells:
        target = cell.target.native_gradle_task
        prop = cell.target.native_image_property
        if target is not None and prop is not None:
            steps.append(
                GradleTask(
                    target,
                    properties={prop: cell.image},
                    executor=executor,
                    role=role,
                    title=f"Build ARM64 {cell.image}",
                )
            )

    return Steps(title=title, steps=tuple(steps))


# ---------------------------------------------------------------------------
# 5. arm64_smoke_composite
# ---------------------------------------------------------------------------


def arm64_smoke_composite(
    plan: Any,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    *,
    title: str = "Test ARM64 images",
    cwd: Path | None = None,
) -> Steps:
    """Run ARM64 server smoke tests on the images that were just built.

    Starts each non-watchdog image as a container and checks its
    health endpoint, then runs the watchdog image and verifies it
    exits as expected when no child runtime is mounted.

    Parameters
    ----------
    plan :
        ImagePlan-like object with a ``cells`` attribute and
        ``target_names``.
    executor :
        Role-bound executor.
    role :
        Execution role.
    title :
        Optional override.
    cwd :
        Working directory.
    """
    # ponytail: simple docker-run smoke per server cell, plus a watchdog
    # check. Inlined from nanolab.release.arm to keep sonata-tasks decoupled.

    server_cells = [c for c in plan.cells if c.target.name != "watchdog"]
    steps: list[Any] = []

    for index, cell in enumerate(server_cells, 1):
        port = 8081 if cell.target.name == "control-plane" else 8080
        health = "/actuator/health" if cell.target.name == "control-plane" else "/health"
        container = f"nanofaas-arm64-smoke-{index}"
        steps.append(
            CommandTask(
                title=f"Smoke {cell.target.name} {cell.flavor}",
                argv=(
                    "docker",
                    "run",
                    "--rm",
                    "-d",
                    "--name",
                    container,
                    "-p",
                    f"{port}:{port}",
                    cell.image,
                ),
                executor=executor,
                role=role,
                cwd=cwd,
            )
        )
        steps.append(
            CommandTask(
                title=f"Health-check {cell.target.name} {cell.flavor}",
                argv=(
                    "curl",
                    "-fsS",
                    "--retry",
                    "10",
                    "--retry-delay",
                    "1",
                    "--retry-connrefused",
                    f"http://127.0.0.1:{port}{health}",
                ),
                executor=executor,
                role=role,
                cwd=cwd,
            )
        )

    # Watchdog smoke — it should fail without a child runtime.
    wd_cells = [c for c in plan.cells if c.target.name == "watchdog"]
    if len(wd_cells) == 1:
        steps.append(
            CommandTask(
                title="Run watchdog smoke",
                argv=("docker", "run", "--rm", wd_cells[0].image, "true"),
                executor=executor,
                role=role,
                cwd=cwd,
                expected_exit_codes=frozenset({1}),
            )
        )

    return Steps(title=title, steps=tuple(steps))


# ---------------------------------------------------------------------------
# 6. publish_architectures_composite
# ---------------------------------------------------------------------------


def publish_architectures_composite(
    plan: Any,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    authfile: str,
    *,
    title: str = "Publish architecture images",
    src_tls_verify: bool = True,
) -> Steps:
    """Copy immutable architecture tags to a production registry.

    Each cell image is copied to its publication destination using
    ``SkopeoCopyTask``.  Digest pinning is left to the registry;
    this composite copies by tag.
    """
    items = _PlanItems(plan)
    if not items.copies:
        raise TypeError("plan must have publishable copies or cells")

    steps: list[Any] = []
    for item in items.copies:
        source_ref = item.source if hasattr(item, "source") else item.image
        dest_ref = item.destination if hasattr(item, "destination") else item.image
        steps.append(
            SkopeoCopyTask(
                source=source_ref,
                destination=dest_ref,
                authfile=authfile,
                executor=executor,
                role=role,
                src_tls_verify=src_tls_verify,
                title=f"Copy {source_ref} -> {dest_ref}",
            )
        )
    return Steps(title=title, steps=tuple(steps))


# ---------------------------------------------------------------------------
# 7. publish_manifests_composite
# ---------------------------------------------------------------------------


def publish_manifests_composite(
    plan: Any,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    docker_config: str,
    *,
    title: str = "Publish multi-architecture manifests",
) -> Steps:
    """Create multi-architecture manifests for every image target.

    Groups cells by target name, then creates a manifest referencing
    the architecture images by tag.  Digest pinning is left to the
    registry.
    """
    items = _PlanItems(plan)
    if items.manifests:
        steps = tuple(
            ImagetoolsCreateTask(
                tag=manifest.reference,
                sources=manifest.sources,
                docker_config=docker_config,
                executor=executor,
                role=role,
                title=f"Create manifest {manifest.reference}",
            )
            for manifest in items.manifests
        )
        return Steps(title=title, steps=steps)

    # ImagePlan path: group cells by target name
    by_target: dict[str, list[Any]] = {}
    for cell in items.copies:
        by_target.setdefault(cell.target.name, []).append(cell)

    arch_steps: list[Any] = []
    for target_name, cells in by_target.items():
        tag_parts = cells[0].image.rsplit(":", 1)
        version_tag = tag_parts[1].rsplit("-", 1)[0]  # remove arch suffix
        registry = tag_parts[0].rsplit("/", 1)[0]
        reference = f"{registry}/{target_name}:{version_tag}"

        arch_steps.append(
            ImagetoolsCreateTask(
                tag=reference,
                sources=tuple(cell.image for cell in cells),
                docker_config=docker_config,
                executor=executor,
                role=role,
                title=f"Create manifest {reference}",
            )
        )
    return Steps(title=title, steps=tuple(arch_steps))


# ---------------------------------------------------------------------------
# 8. publish_aliases_composite
# ---------------------------------------------------------------------------


def publish_aliases_composite(
    plan: Any,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    docker_config: str,
    *,
    title: str = "Publish version aliases",
) -> Steps:
    """Point mutable version aliases at their manifests.

    Uses ``ImagetoolsCreateTask`` to tag a manifest with an alias
    (e.g. ``1.0.0`` pointing to ``1.0.0-native``).  Digest pinning
    is left to the registry.
    """
    items = _PlanItems(plan)
    if not items.aliases:
        return Steps(
            title=title,
            steps=(
                CommandTask(
                    title="No aliases to publish",
                    argv=("true",),
                    executor=executor,
                    role=role,
                ),
            ),
        )

    steps = tuple(
        ImagetoolsCreateTask(
            tag=alias.reference,
            sources=(alias.source,),
            docker_config=docker_config,
            executor=executor,
            role=role,
            title=f"Create alias {alias.reference} -> {alias.source}",
        )
        for alias in items.aliases
    )
    return Steps(title=title, steps=steps)


# ---------------------------------------------------------------------------
# 9. attest_composite
# ---------------------------------------------------------------------------


class _AttestImageTask(ReusableTask):
    """One digest's whole attestation, skippable as a unit on resume.

    A nested ``Steps`` of plain ``Task``s does not skip: ``decide_resume``
    returns ``"skip"`` only for a ``ReusableTask`` whose evidence still
    verifies, and ``CommandTask``/``CosignTask``/``SyftTask`` are plain tasks.
    This class is that ``ReusableTask``, so a resumed release re-signs the
    digest it died on and leaves the ones already signed alone. The six
    operations stay a nested ``Steps`` beneath it, so the journal still names
    each one.

    The outcome carries the ``cosign-attestation`` evidence for this digest and
    no value: a skipped step may only contribute ``None``.
    """

    # A resumed group must be able to retry the operation it died on, and a
    # `failed` record on a non-idempotent task raises instead of retrying.
    idempotent = True

    def __init__(
        self,
        *,
        image: str,
        steps: tuple[Task[Any], ...],
        identity: Mapping[str, Any],
        signed: list[Evidence],
    ) -> None:
        self._image = image
        self._steps = steps
        self._signed = signed
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        self._reuse_key = f"attest:{image}:sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"
        # The reuse key rides in the title because a step's journal identity is
        # its slug, and this composite is built inside a release phase at run
        # time -- so it never reaches `CompiledWorkflow.fingerprint`, the only
        # place the engine ever consults a `reuse_key`. Without it, a changed
        # predicate path or operation set would skip on a record that no longer
        # describes this work.
        self.title = f"Attest {image} {self._reuse_key[-8:]}"
        self._group = Steps(title=self.title, steps=steps)

    @property
    def reuse_key(self) -> str:
        return self._reuse_key

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        self._group.run(inputs)
        evidence = Evidence("cosign-attestation", self._image, self._image.split("@", 1)[1])
        self._signed.append(evidence)
        return TaskOutcome(evidence=(evidence,))


def attest_composite(
    images: Sequence[str],
    *,
    predicate_remote: str,
    sbom_dir_remote: str,
    public_key_remote: str,
    cosign_key: str,
    password_file: str,
    docker_config: str,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    signed: list[Evidence] | None = None,
    title: str = "Attest images",
) -> Steps:
    """SBOM, sign, attest, attach and verify every digest, one group per image.

    The per-image grouping is the point: this phase issues six container runs
    per digest across the whole published matrix, and a network failure two
    thirds of the way through should resume from the digest it died on, not
    from the first one. Each group is a `ReusableTask`, which is what makes the
    engine skip it -- see `_AttestImageTask`.

    `signed` collects one `cosign-attestation` Evidence per group that actually
    ran, in run order, so a caller can record what this run signed rather than
    what it set out to sign. A group the journal skipped appends nothing: it
    was signed by an earlier run, which recorded it then.

    `verify` and `verify-attestation` are both included, matching
    `attest_release_images`: `verify` checks the simple-signing signature
    `sign` produced, `verify-attestation` checks the separate in-toto
    attestation `attest` produced. One passing says nothing about the other,
    so dropping either loses real coverage.

    `images` must be digest-pinned references (``repo/name@sha256:...``).
    Signing a tag signs whatever the tag points at when cosign resolves it,
    which is not necessarily what the release verified.

    `public_key_remote` must already hold the public half of `cosign_key`:
    ``cosign verify`` rejects an encrypted private key outright. Deriving it is
    a one-shot setup step, so it belongs to the caller, not to this per-image
    composite.
    """
    if not images:
        return Steps(
            title=title,
            steps=(
                CommandTask(
                    title="No images to attest", argv=("true",), executor=executor, role=role
                ),
            ),
        )

    collected = [] if signed is None else signed
    cell_steps: list[Any] = []
    for image in images:
        if "@" not in image:
            raise ValueError(f"attestation needs a digest-pinned reference, got {image!r}")
        sbom_path = f"{sbom_dir_remote}/{_artifact_slug(image)}.spdx.json"
        operations: tuple[Task[Any], ...] = (
            SyftTask(
                image=image,
                output_path=sbom_path,
                docker_config=docker_config,
                executor=executor,
                role=role,
            ),
            CosignTask(
                operation="sign",
                image=image,
                key_file=cosign_key,
                password_file=password_file,
                docker_config=docker_config,
                executor=executor,
                role=role,
            ),
            CosignTask(
                operation="attest",
                image=image,
                key_file=cosign_key,
                password_file=password_file,
                docker_config=docker_config,
                predicate_file=predicate_remote,
                executor=executor,
                role=role,
            ),
            CosignTask(
                operation="attach sbom",
                image=image,
                key_file=cosign_key,
                password_file=password_file,
                docker_config=docker_config,
                sbom_file=sbom_path,
                executor=executor,
                role=role,
            ),
            CosignTask(
                operation="verify",
                image=image,
                key_file=cosign_key,
                password_file=password_file,
                docker_config=docker_config,
                public_key_file=public_key_remote,
                executor=executor,
                role=role,
            ),
            CosignTask(
                operation="verify-attestation",
                image=image,
                key_file=cosign_key,
                password_file=password_file,
                docker_config=docker_config,
                public_key_file=public_key_remote,
                executor=executor,
                role=role,
            ),
        )
        for step in operations:
            # The six are all safe to re-enter: syft regenerates, cosign
            # sign/attest/attach upsert, verify is read-only. Saying so is what
            # lets a resumed group retry the operation it died on instead of
            # refusing the resume outright.
            step.idempotent = True
        cell_steps.append(
            _AttestImageTask(
                image=image,
                steps=operations,
                identity={
                    "schema": 1,
                    "image": image,
                    # Titles carry the operation and the reference, so a
                    # dropped, renamed or reordered operation changes the key.
                    "operations": [step.title for step in operations],
                    "predicate": predicate_remote,
                    "sbom": sbom_path,
                    "publicKey": public_key_remote,
                    "key": cosign_key,
                    "dockerConfig": docker_config,
                },
                signed=collected,
            )
        )
    return Steps(title=title, steps=tuple(cell_steps))


def _artifact_slug(reference: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", reference.split("/")[-1])
