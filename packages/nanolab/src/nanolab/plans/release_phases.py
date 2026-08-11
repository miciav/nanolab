"""One function per release phase.

`build_release_workflow` reached 703 lines and 51 live locals, which is more
than a reader can hold at once and is why every change there costs a full
re-read. The phases were already there in the comments; this gives them names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sonata_tasks.release_composites import command_specs_composite, registry_push_composite
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor

from nanolab.images.plan import ImagePlan
from nanolab.release.build import amd64_build_commands, source_test_commands
from nanolab.release.model import ReleaseIdentity
from nanolab.release.resources import (
    ReleaseResources,
    ReleaseSourceResources,
    build_release_source_resources,
)
from nanolab.release.tasks import (
    ReleasePhaseTask,
    amd64_build_task,
    registry_push_task,
    run_image_steps,
    run_source_steps,
    source_test_task,
)


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
                    tuple(sorted(command.env.items())),
                    str(command.remote_dir),
                    str(command.cwd),
                    command.timeout_seconds,
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
                (command.argv, command.role, str(command.remote_dir))
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
