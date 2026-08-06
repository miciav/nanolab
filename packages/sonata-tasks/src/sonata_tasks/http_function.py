from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from sonata_engine import Resource
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import TaskResult

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


def _split_response(stdout: str) -> tuple[str, str]:
    """Headers and body of a `curl -i` response, split at the blank line.

    A header block ends with CRLF CRLF; tolerating LF LF as well costs nothing
    and saves a puzzling failure against anything that does not send CRLF.
    """
    for separator in ("\r\n\r\n", "\n\n"):
        head, found, body = stdout.partition(separator)
        if found:
            return head, body
    return "", stdout


class HttpFunctionInvokeTask(CommandTask):
    """Invoke a function over the control plane API and check what came back.

    The response is verified rather than merely accepted: a 200 carrying an
    error status is a failed invocation, and curl alone cannot tell.

    `require_header` additionally demands a header be present — for offload,
    where the point of the invocation is that it was proxied, and a perfectly
    valid answer computed locally would be the failure. Asking for one switches
    curl to `-i`, so the headers arrive in the same stdout as the body: the
    shell version wrote them to a `mktemp` file and grepped it, which needed a
    trap to clean up and could not tell a missing header from a failed grep.
    """

    def __init__(
        self,
        name: str,
        *,
        payload: str,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        require_header: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        def verify(result: TaskResult) -> None:
            if require_header is None:
                verify_invocation(result)
                return
            headers, body = _split_response(result.stdout)
            names = [line.split(":", 1)[0].strip().lower() for line in headers.splitlines()]
            if require_header.lower() not in names:
                raise RuntimeError(
                    f"{name}: response carried no {require_header} header; "
                    f"got {sorted(name for name in names if name)}"
                )
            verify_invocation(replace(result, stdout=body))

        super().__init__(
            title=f"Invoke {name}",
            argv=_argv(
                endpoint,
                lambda base: (
                    "curl",
                    "-fsS" if require_header is None else "-isS",
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
            verify=verify,
        )


class HttpStatusCheckTask(CommandTask):
    """Assert a request answers with an exact status code.

    For the cases where the status *is* the assertion: offload's last check
    removes the remote function and requires the edge to answer 502, because a
    single hop with no local fallback is the contract. A 200 there would mean
    the edge quietly computed it itself.

    `-o /dev/null -w %{http_code}` rather than `-f`: a 502 must be read, not
    turned into a non-zero exit that says only that something went wrong.
    """

    def __init__(
        self,
        *,
        url: str,
        expected_status: int,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        payload: str | None = None,
        title: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        def check(result: TaskResult) -> None:
            actual = result.stdout.strip()
            if actual != str(expected_status):
                raise RuntimeError(f"{url}: answered {actual or 'nothing'}, expected {expected_status}")

        data = ("-H", "Content-Type: application/json", "--data", payload) if payload else ()
        super().__init__(
            title=title or f"Expect {expected_status} from {url}",
            argv=("curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", *data, url),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=check,
        )
