from __future__ import annotations

import shlex
from pathlib import Path

from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole

from sonata_tasks.command import CommandTask
from sonata_tasks.invocation import verify_invocation
from sonata_tasks.manifest import FunctionManifest


def _apply_script(manifest: FunctionManifest, cli_argv: tuple[str, ...]) -> str:
    """Shell that writes the manifest next to the CLI and applies it.

    The file is created with `mktemp` on the target on purpose. Writing it here
    with `tempfile` would work when the CLI runs on this host and silently break
    when it runs inside a VM, which would never see a local path.
    """
    apply_command = " ".join(
        shlex.quote(value) for value in (*cli_argv, "fn", "apply", "--file", "$manifest")
    ).replace("'$manifest'", '"$manifest"')
    return (
        f"manifest=$(mktemp); trap 'rm -f \"$manifest\"' EXIT; "
        f"printf '%s' {shlex.quote(manifest.json())} > \"$manifest\"; " + apply_command
    )


class CliFunctionApplyTask(CommandTask):
    """Register a function by applying a manifest through the nanofaas CLI.

    `cli_argv` is the invocation prefix — binary plus its global flags — so this
    task stays out of the business of knowing how the CLI is addressed.
    """

    def __init__(
        self,
        manifest: FunctionManifest,
        *,
        cli_argv: tuple[str, ...],
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Apply {manifest.name}",
            argv=("bash", "-lc", _apply_script(manifest, cli_argv)),
            executor=executor,
            role=role,
            cwd=cwd,
        )


class CliFunctionDeleteTask(CommandTask):
    """Remove a function through the nanofaas CLI.

    Only exit 0 is accepted: the CLI already exits 0 when the function is absent,
    so tolerating 1 as well would hide genuine cleanup failures.
    """

    def __init__(
        self,
        name: str,
        *,
        cli_argv: tuple[str, ...],
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Delete {name}",
            argv=(*cli_argv, "fn", "delete", name),
            executor=executor,
            role=role,
            cwd=cwd,
        )


class CliFunctionInvokeTask(CommandTask):
    """Invoke a function through the nanofaas CLI and check what came back.

    The CLI's twin of `HttpFunctionInvokeTask`: different transport, same
    response, so both share `verify_invocation`.
    """

    def __init__(
        self,
        name: str,
        *,
        payload: str,
        cli_argv: tuple[str, ...],
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Invoke {name}",
            argv=(*cli_argv, "invoke", name, "--data", payload),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify_invocation,
        )
