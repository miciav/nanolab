from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import TaskResult

from sonata_tasks.docker import DockerTask


SYFT_IMAGE = (
    "anchore/syft@sha256:"
    "f94e5d9fce1f2278491a8e3a63bd5f6ddb81fdfdbb8bf7a1637565c1d5344357"
)


class SyftTask(DockerTask):
    """Generate an SPDX SBOM for a container image using ``anchore/syft``.

    Runs the pinned syft image via ``docker run``, mounting the Docker config
    directory for registry authentication and the output directory for the SPDX
    file.
    """

    def __init__(
        self,
        *,
        image: str,
        output_path: str,
        docker_config: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        title: str | None = None,
        cwd: Path | None = None,
        verify: Callable[[TaskResult], None] | None = None,
    ) -> None:
        output_dir = str(Path(output_path).parent)
        output_file = Path(output_path).name
        super().__init__(
            "run", "--rm",
            "--env", "DOCKER_CONFIG=/auth",
            "--volume", f"{docker_config}:/auth:ro",
            "--volume", f"{output_dir}:/out",
            SYFT_IMAGE, image,
            "-o", f"spdx-json=/out/{output_file}",
            executor=executor,
            role=role,
            title=title or f"Syft SBOM {image}",
            cwd=cwd,
            verify=verify,
        )
