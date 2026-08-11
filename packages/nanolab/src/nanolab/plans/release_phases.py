"""One function per release phase.

`build_release_workflow` reached 703 lines and 51 live locals, which is more
than a reader can hold at once and is why every change there costs a full
re-read. The phases were already there in the comments; this gives them names.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sonata_tasks.release_composites import command_specs_composite, registry_push_composite
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor

from nanolab.images.plan import ImagePlan
from nanolab.plans import loadtest as loadtest_plan
from nanolab.release.benchmark import (
    performance_profile,
    regression_policy,
    run_sonata_aggregate,
    run_sonata_benchmark,
    run_sonata_regression_gate,
)
from nanolab.release.model import digest_path
from nanolab.release.build import amd64_build_commands, source_test_commands
from nanolab.release.model import ReleaseIdentity

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from nanolab.plans.release import ReleaseRequest
from nanolab.release.resources import (
    ReleaseResources,
    ReleaseSourceResources,
    build_release_source_resources,
)
from nanolab.release.tasks import (
    ReleasePhaseTask,
    aggregate_benchmarks_task,
    amd64_build_task,
    benchmark_task,
    regression_gate_task,
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
