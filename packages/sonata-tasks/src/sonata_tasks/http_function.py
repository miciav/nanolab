from __future__ import annotations

import json
from pathlib import Path

from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole

from sonata_tasks.command import CommandTask
from sonata_tasks.manifest import FunctionManifest

# Reaching the control plane over HTTP, for workflows that talk to its API rather
# than drive the nanofaas CLI. `role` is required throughout: curling from the
# host and curling from inside a VM reach different network namespaces, and the
# endpoint that works in one is usually wrong in the other.


def _curl(*arguments: str, through_shell: bool) -> tuple[str, ...]:
    """Wrap curl for a remote role, where the whole command crosses a shell.

    A VM-bound executor hands the argv to a login shell, so every argument has to
    survive one round of word splitting — hence the JSON quoting rather than
    passing the list through untouched.
    """
    if not through_shell:
        return ("curl", *arguments)
    return ("bash", "-lc", " ".join(json.dumps(argument) for argument in ("curl", *arguments)))


class HttpFunctionRegisterTask(CommandTask):
    """POST a function manifest to the control plane."""

    def __init__(
        self,
        manifest: FunctionManifest,
        *,
        endpoint: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        through_shell: bool = False,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Register {manifest.name}",
            argv=_curl(
                "-fsS",
                "-H",
                "Content-Type: application/json",
                "--data",
                manifest.json(),
                f"{endpoint}/v1/functions",
                through_shell=through_shell,
            ),
            executor=executor,
            role=role,
            cwd=cwd,
        )


class HttpFunctionDeleteTask(CommandTask):
    """DELETE a function from the control plane.

    Tolerates curl's "could not connect" (7) and "HTTP error" (22) so cleanup
    stays idempotent: the function may already be gone, or the control plane may
    have been torn down first.
    """

    def __init__(
        self,
        name: str,
        *,
        endpoint: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        through_shell: bool = False,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Delete {name}",
            argv=_curl(
                "-fsS",
                "-X",
                "DELETE",
                f"{endpoint}/v1/functions/{name}",
                through_shell=through_shell,
            ),
            executor=executor,
            role=role,
            cwd=cwd,
            expected_exit_codes=frozenset({0, 7, 22}),
        )
