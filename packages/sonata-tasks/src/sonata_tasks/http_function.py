from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sonata_engine import Resource
from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole

from sonata_tasks.command import Argv, CommandTask
from sonata_tasks.invocation import verify_invocation
from sonata_tasks.manifest import FunctionManifest

# Reaching the control plane over HTTP, for workflows that talk to its API rather
# than drive the nanofaas CLI. `role` is required throughout: curling from the
# host and curling from inside a VM reach different network namespaces, and the
# endpoint that works in one is usually wrong in the other.

Endpoint = str | Resource[str]
"""Where the control plane answers.

A plain string when the workflow knows it up front, as the container backend
does. A `Resource[str]` when it exists only once something has been created: on
Kubernetes the address comes from the Service the Helm release installs, so it
cannot be spelled before that release is acquired.

The shell version solved the same problem by embedding `$(kubectl get service
...)` in the URL and running every curl through `bash -lc`, which then meant
JSON-quoting each argument to survive the extra round of word splitting. Reading
the value from a resource removes the subshell, and with it the reason for the
shell — every executor already quotes the argv it is handed.
"""


def _argv(endpoint: Endpoint, build: Callable[[str], tuple[str, ...]]) -> Argv:
    if isinstance(endpoint, str):
        return build(endpoint)
    return lambda inputs: build(inputs.resource(endpoint))


class HttpFunctionRegisterTask(CommandTask):
    """POST a function manifest to the control plane."""

    def __init__(
        self,
        manifest: FunctionManifest,
        *,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Register {manifest.name}",
            argv=_argv(
                endpoint,
                lambda base: (
                    "curl",
                    "-fsS",
                    "-H",
                    "Content-Type: application/json",
                    "--data",
                    manifest.json(),
                    f"{base}/v1/functions",
                ),
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
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Delete {name}",
            argv=_argv(
                endpoint,
                lambda base: ("curl", "-fsS", "-X", "DELETE", f"{base}/v1/functions/{name}"),
            ),
            executor=executor,
            role=role,
            cwd=cwd,
            expected_exit_codes=frozenset({0, 7, 22}),
        )


class HttpFunctionInvokeTask(CommandTask):
    """Invoke a function over the control plane API and check what came back.

    The response is verified rather than merely accepted: a 200 carrying an
    error status is a failed invocation, and curl alone cannot tell.
    """

    def __init__(
        self,
        name: str,
        *,
        payload: str,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Invoke {name}",
            argv=_argv(
                endpoint,
                lambda base: (
                    "curl",
                    "-fsS",
                    "-H",
                    "Content-Type: application/json",
                    "--data",
                    payload,
                    f"{base}/v1/functions/{name}:invoke",
                ),
            ),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify_invocation,
        )
