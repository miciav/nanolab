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

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sonata_engine import Steps
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
    "source_tests_composite",
    "amd64_build_composite",
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
            self.manifests = (
                plan.manifests if hasattr(plan, "manifests") else ()
            )
            self.aliases = (
                plan.aliases if hasattr(plan, "aliases") else ()
            )


# ---------------------------------------------------------------------------
# 1. source_tests_composite
# ---------------------------------------------------------------------------


def source_tests_composite(
    commands: Sequence[CommandTaskSpec],
    executor: CommandTaskExecutor,
    *,
    title: str = "Run source tests",
) -> Steps:
    """Wrap a list of test commands into a Steps composite.

    Each ``CommandTaskSpec`` becomes a ``CommandTask``, run in order.
    The composite is the simplest wrapper — a flat sequence, no
    cell-level grouping.

    Parameters
    ----------
    commands :
        One spec per test command.  ``spec.summary`` becomes the
        task title, ``spec.argv`` the command line, ``spec.role``
        the execution role.
    executor :
        Role-bound executor that runs each command.
    title :
        Optional override for the composite title (default
        ``"Run source tests"``).
    """
    steps = tuple(
        CommandTask(
            title=spec.summary or f"source-test-{i}",
            argv=spec.argv,
            executor=executor,
            role=spec.role or "stack",
        )
        for i, spec in enumerate(commands)
    )
    return Steps(title=title, steps=steps)


# ---------------------------------------------------------------------------
# 2. amd64_build_composite
# ---------------------------------------------------------------------------


def amd64_build_composite(
    plan: Any,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    *,
    title: str = "Build AMD64 images",
    cwd: Path | None = None,
) -> Steps:
    """Build every AMD64 image cell.

    Iterates ``plan.cells``, selecting those whose architecture is
    ``"amd64"``.

    * **Bake cells** (``build_kind == "bake"``) — built with
      ``DockerBuildTask`` using the cell's Dockerfile and context.
    * **Gradle cells** (``build_kind == "gradle"``) — built with
      ``GradleTask`` using the cell's native Gradle task and image
      property.

    Parameters
    ----------
    plan :
        An object whose ``cells`` attribute is a sequence of
      ``ImageCell``-like objects.
    executor :
        Role-bound executor.
    role :
        Execution role (typically ``"host"`` for local AMD64).
    title :
        Optional override.
    cwd :
        Working directory for each build command.
    """
    steps: list[Any] = []
    for cell in plan.cells:
        if cell.architecture != "amd64":
            continue
        if cell.build_kind == "gradle":
            target = cell.target.native_gradle_task
            prop = cell.target.native_image_property
            if target is None or prop is None:
                raise ValueError(f"missing Gradle metadata for {cell.target.name}")
            steps.append(
                GradleTask(
                    target,
                    properties={prop: cell.image},
                    executor=executor,
                    role=role,
                    title=f"Build {cell.image}",
                    cwd=cwd,
                )
            )
        else:
            steps.append(
                DockerBuildTask(
                    image=cell.image,
                    dockerfile=str(cell.target.dockerfile),
                    context=str(cell.target.context),
                    executor=executor,
                    role=role,
                    title=f"Build {cell.image}",
                    cwd=cwd,
                )
            )
    return Steps(title=title, steps=tuple(steps))


# ---------------------------------------------------------------------------
# 3. registry_push_composite
# ---------------------------------------------------------------------------


def registry_push_composite(
    plan: Any,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    authfile: str = "",
    *,
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
    title :
        Optional override.
    """
    cell_steps = tuple(
        Steps(
            title=f"Push and inspect {cell.image}",
            steps=(
                DockerPushTask(
                    image=cell.image, executor=executor, role=role,
                ),
                SkopeoInspectTask(
                    reference=cell.image,
                    authfile=authfile,
                    executor=executor,
                    role=role,
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

    Sets up a registry tunnel, creates and bootstraps a buildx
    builder, runs the bake, then builds each Gradle native image.
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

    # ponytail: registry tunnel is handled by registry_tunnel_resource in the
    # parent workflow DAG — the composite assumes it is already running.
    steps.append(
        CommandTask(
            title="Create ARM64 buildx builder",
            argv=("docker", "buildx", "create", "--name", builder_name,
                   "--driver", "docker-container", "--use"),
            executor=executor,
            role=role,
            cwd=Path(remote_source_dir) if remote_source_dir else None,
        )
    )
    steps.append(
        CommandTask(
            title="Bootstrap ARM64 buildx builder",
            argv=("docker", "buildx", "inspect", builder_name, "--bootstrap"),
            executor=executor,
            role=role,
            cwd=Path(remote_source_dir) if remote_source_dir else None,
        )
    )

    arm64_cells = [c for c in plan.cells if c.architecture == "arm64"]
    arm64_bake_cells = [c for c in arm64_cells if c.build_kind == "bake"]
    arm64_gradle_cells = [c for c in arm64_cells if c.build_kind == "gradle"]

    if arm64_bake_cells:
        if remote_bake_file:
            steps.append(
                CommandTask(
                    title="Bake ARM64 Dockerfile images",
                    argv=("docker", "buildx", "bake", "--builder", builder_name,
                           "--file", remote_bake_file, "--load", "docker-arm64"),
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
                title=f"Smoke {cell.target.name}",
                argv=(
                    "docker", "run", "--rm", "-d",
                    "--name", container,
                    "-p", f"{port}:{port}",
                    cell.image,
                ),
                executor=executor,
                role=role,
                cwd=cwd,
            )
        )
        steps.append(
            CommandTask(
                title=f"Health-check {cell.target.name}",
                argv=(
                    "curl", "-fsS", "--retry", "10", "--retry-delay", "1",
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


def attest_composite(
    images: Sequence[str],
    predicate_remote: Path,
    sbom_dir_remote: Path,
    cosign_key: str,
    docker_config: str,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    *,
    title: str = "Attest images",
    password_file: str = "",
) -> Steps:
    """Generate SBOMs and cosign attestations for a list of images.

    For each image: generate an SPDX SBOM with ``SyftTask``, then
    cosign attest with ``CosignTask``.

    Parameters
    ----------
    images :
        Image references to attest.
    predicate_remote :
        Path to the cosign predicate file on each remote.
    sbom_dir_remote :
        Directory on each remote where the SBOM will be written.
    cosign_key :
        Path to the cosign private key on each remote.
    docker_config :
        Path to the Docker config directory.
    executor :
        Role-bound executor.
    role :
        Execution role.
    title :
        Optional override.
    password_file :
        Path to the file containing the cosign key password.
    """
    if not images:
        return Steps(
            title=title,
            steps=(
                CommandTask(title="No images to attest", argv=("true",), executor=executor, role=role),
            ),
        )

    cell_steps: list[Any] = []
    for image in images:
        sbom_path = sbom_dir_remote / f"{image.replace('/', '_').replace(':', '_')}.spdx.json"
        cell_steps.append(
            Steps(
                title=f"Attest {image}",
                steps=(
                    SyftTask(
                        image=image,
                        output_path=str(sbom_path),
                        docker_config=docker_config,
                        executor=executor,
                        role=role,
                    ),
                    CosignTask(
                        operation="attest",
                        image=image,
                        key_file=cosign_key,
                        password_file=password_file or cosign_key + ".password",
                        docker_config=docker_config,
                        executor=executor,
                        role=role,
                        predicate_file=str(predicate_remote),
                    ),
                ),
            )
        )
    return Steps(title=title, steps=tuple(cell_steps))
