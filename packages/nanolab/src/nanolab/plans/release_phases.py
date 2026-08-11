"""One function per release phase.

`build_release_workflow` reached 703 lines and 51 live locals, which is more
than a reader can hold at once and is why every change there costs a full
re-read. The phases were already there in the comments; this gives them names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sonata_tasks.release_composites import command_specs_composite
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor

from nanolab.release.build import source_test_commands
from nanolab.release.model import ReleaseIdentity
from nanolab.release.resources import (
    ReleaseResources,
    ReleaseSourceResources,
    build_release_source_resources,
)
from nanolab.release.tasks import (
    ReleasePhaseTask,
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
