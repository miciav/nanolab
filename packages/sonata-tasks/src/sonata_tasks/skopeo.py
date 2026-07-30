from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask


class SkopeoCopyTask(CommandTask):
    """Copy a container image between registries via skopeo copy.

    Preserves digests by default. TLS verification for the *source* registry
    can be disabled with ``src_tls_verify=False`` for registries using
    self-signed certificates or plain HTTP.
    """

    def __init__(
        self,
        *,
        source: str,
        destination: str,
        authfile: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        src_tls_verify: bool = True,
        title: str | None = None,
        cwd: Path | None = None,
        verify: Callable[[TaskResult], None] | None = None,
    ) -> None:
        argv: list[str] = ["skopeo", "copy", "--preserve-digests"]
        if not src_tls_verify:
            argv.append("--src-tls-verify=false")
        argv.extend(
            [
                "--dest-authfile",
                authfile,
                f"docker://{source}",
                f"docker://{destination}",
            ]
        )
        super().__init__(
            title=title or f"Copy image {source} -> {destination}",
            argv=tuple(argv),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify,
        )


class SkopeoInspectTask(CommandTask):
    """Read a remote image's digest via skopeo inspect.

    The ``--format={{.Digest}}`` template produces only the digest string so
    callers can read it from ``result.stdout``. TLS verification can be
    disabled with ``tls_verify=False`` for registries using self-signed
    certificates.
    """

    def __init__(
        self,
        *,
        reference: str,
        authfile: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        tls_verify: bool = True,
        title: str | None = None,
        cwd: Path | None = None,
        verify: Callable[[TaskResult], None] | None = None,
    ) -> None:
        argv: list[str] = [
            "skopeo",
            "inspect",
            "--format={{.Digest}}",
            "--authfile",
            authfile,
        ]
        if not tls_verify:
            argv.append("--tls-verify=false")
        argv.append(f"docker://{reference}")
        super().__init__(
            title=title or f"Inspect {reference}",
            argv=tuple(argv),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify,
        )
