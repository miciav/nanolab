from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, replace
import json
from pathlib import Path
from time import monotonic, sleep
from typing import cast

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
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

_JSON_CONTENT_TYPE_HEADER = "Content-Type: application/json"
_CONTENT_TYPE_HEADER_NAME = "content-type"


def _argv(endpoint: Endpoint, build: Callable[[str], tuple[str, ...]]) -> Argv:
    if isinstance(endpoint, str):
        return build(endpoint)
    return lambda inputs: build(inputs.resource(endpoint))



def _command_result(outcome: TaskOutcome[TaskResult], what: str) -> TaskResult:
    """A CommandTask always carries its result; the outcome type allows None.

    Saying so here turns what would be an AttributeError on None into a sentence
    that names the command.
    """
    if outcome.value is None:
        raise RuntimeError(f"{what}: command produced no result")
    return outcome.value


def _verify_registration_response(
    manifest: FunctionManifest,
    stdout: str,
    expected_backend: str | None,
    expected_endpoint_prefix: str | None,
) -> None:
    """Assert the control plane echoed the manifest and honored runtime expectations.

    The backend and endpoint prefix are the deployment-derived values a workflow
    pins down; when both are None the response needs no checks beyond parsing.
    """
    try:
        response = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{manifest.name}: invalid registration response") from error
    for field, value in {
        "name": manifest.name,
        "image": manifest.image,
        "requestedExecutionMode": manifest.execution_mode,
        "effectiveExecutionMode": manifest.execution_mode,
    }.items():
        if response.get(field) != value:
            raise RuntimeError(
                f"{manifest.name}: {field} was {response.get(field)!r}, expected {value!r}"
            )
    if expected_backend is not None and response.get("deploymentBackend") != expected_backend:
        raise RuntimeError(
            f"{manifest.name}: deploymentBackend was {response.get('deploymentBackend')!r}, "
            f"expected {expected_backend!r}"
        )
    _verify_registration_endpoint(
        manifest.name, response.get("endpointUrl"), expected_endpoint_prefix
    )


def _verify_registration_endpoint(
    name: str, endpoint_url: object, expected_prefix: str | None
) -> None:
    """The control plane's assigned endpoint must carry the expected prefix."""
    if expected_prefix is not None and (
        not isinstance(endpoint_url, str) or not endpoint_url.startswith(expected_prefix)
    ):
        raise RuntimeError(
            f"{name}: endpointUrl was {endpoint_url!r}, expected prefix {expected_prefix!r}"
        )


class HttpFunctionRegisterTask(CommandTask):
    """POST a function manifest to the control plane."""

    def __init__(
        self,
        manifest: FunctionManifest,
        *,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        expected_backend: str | None = None,
        expected_endpoint_prefix: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        def verify(result: TaskResult) -> None:
            if expected_backend is None and expected_endpoint_prefix is None:
                return
            _verify_registration_response(
                manifest, result.stdout, expected_backend, expected_endpoint_prefix
            )

        super().__init__(
            title=f"Register {manifest.name}",
            argv=_argv(
                endpoint,
                lambda base: (
                    "curl",
                    "-sS",
                    "--fail-with-body",
                    "--retry",
                    "10",
                    "--retry-delay",
                    "1",
                    "--retry-max-time",
                    "30",
                    "-H",
                    _JSON_CONTENT_TYPE_HEADER,
                    "--data",
                    manifest.json(),
                    f"{base}/v1/functions",
                ),
            ),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify if expected_backend is not None or expected_endpoint_prefix is not None else None,
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


class HttpFunctionEnqueueTask(Task[str]):
    """Enqueue one invocation and return its execution id.

    When ``match_upstream`` is set, the immediately preceding enqueue must have
    returned the same id.  A ``Steps`` composite uses that to prove the public
    idempotency contract without persisting state outside the workflow.
    """

    def __init__(
        self,
        name: str,
        *,
        payload: str,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        idempotency_key: str,
        match_upstream: bool = False,
        title: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.title = title or f"Enqueue {name}"
        self._name = name
        self._match_upstream = match_upstream
        self._command = CommandTask(
            title=self.title,
            argv=_argv(
                endpoint,
                lambda base: (
                    "curl",
                    "-fsS",
                    "-H",
                    _JSON_CONTENT_TYPE_HEADER,
                    "-H",
                    f"Idempotency-Key: {idempotency_key}",
                    "--data",
                    payload,
                    f"{base}/v1/functions/{name}:enqueue",
                ),
            ),
            executor=executor,
            role=role,
            cwd=cwd,
        )

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        result = _command_result(self._command.run(inputs), self._name)
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"{self._name}: invalid enqueue response") from error
        execution_id = response.get("executionId")
        expected_statuses = {"queued", "success"} if self._match_upstream else {"queued"}
        if response.get("status") not in expected_statuses or not isinstance(execution_id, str) or not execution_id:
            raise RuntimeError(f"{self._name}: expected an accepted execution, got {response!r}")
        if self._match_upstream and inputs.upstream() != execution_id:
            raise RuntimeError(
                f"{self._name}: idempotent enqueue returned {execution_id!r}, "
                f"expected {inputs.upstream()!r}"
            )
        return TaskOutcome(value=execution_id)


class _UnsetType:
    __slots__ = ()


_UNSET = _UnsetType()


class HttpExecutionSuccessTask(Task[None]):
    """Poll the execution id from the preceding enqueue until it succeeds.

    When `expected_output` or `expected_status_code` is given, the final success
    response must also carry the matching `output`/`statusCode`, so an async
    load run proves the function processed its payload, not merely that it ran.
    """

    def __init__(
        self,
        *,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        timeout_seconds: float = 20,
        poll_seconds: float = 0.5,
        expected_output: object = _UNSET,
        expected_status_code: object = _UNSET,
        cwd: Path | None = None,
    ) -> None:
        self.title = "Wait for enqueued execution"
        self._endpoint = endpoint
        self._executor = executor
        self._role: ExecutionRole = role
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._expected_output = expected_output
        self._expected_status_code = expected_status_code
        self._cwd = cwd

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        execution_id = inputs.upstream()
        if not isinstance(execution_id, str) or not execution_id:
            raise RuntimeError(f"{self.title}: expected an execution id from enqueue")
        command = CommandTask(
            title=self.title,
            argv=_argv(self._endpoint, lambda base: ("curl", "-fsS", f"{base}/v1/executions/{execution_id}")),
            executor=self._executor,
            role=self._role,
            cwd=self._cwd,
        )
        deadline = monotonic() + self._timeout_seconds
        while True:
            result = _command_result(command.run(inputs), self.title)
            try:
                response = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"{self.title}: invalid execution response") from error
            if response.get("executionId") != execution_id:
                raise RuntimeError(f"{self.title}: response was for {response.get('executionId')!r}")
            if response.get("status") == "success":
                if (
                    self._expected_output is not _UNSET
                    and response.get("output") != self._expected_output
                ):
                    raise RuntimeError(
                        f"{self.title}: output was {response.get('output')!r}, "
                        f"expected {self._expected_output!r}"
                    )
                if (
                    self._expected_status_code is not _UNSET
                    and response.get("statusCode") != self._expected_status_code
                ):
                    raise RuntimeError(
                        f"{self.title}: statusCode was {response.get('statusCode')!r}, "
                        f"expected {self._expected_status_code!r}"
                    )
                return TaskOutcome(value=None)
            if response.get("status") in {"error", "timeout"}:
                raise RuntimeError(f"{self.title}: execution ended {response.get('status')}")
            if monotonic() >= deadline:
                raise RuntimeError(f"{self.title}: execution {execution_id!r} did not succeed in time")
            sleep(self._poll_seconds)


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


def _split_final_response(stdout: str) -> tuple[str, str]:
    """Return curl's last HTTP block, skipping informational and redirect blocks."""
    headers, body = _split_response(stdout)
    while body.startswith("HTTP/"):
        headers, body = _split_response(body)
    return headers, body


@dataclass(frozen=True)
class HttpFunctionExpectation:
    """The complete externally visible function-response contract to assert."""

    status: int
    api_status: str | None | _UnsetType = _UNSET
    output: object = _UNSET
    status_code: int | None | _UnsetType = _UNSET
    api_headers: dict[str, str] | None | _UnsetType = _UNSET
    encoding: str | None | _UnsetType = _UNSET
    required_headers: tuple[tuple[str, str], ...] = ()
    forbidden_headers: tuple[str, ...] = ()
    forbidden_header_values: tuple[tuple[str, str], ...] = ()
    decoded_bytes: bytes | None = None
    decoded_prefix: bytes | None = None


def _parse_contract_response(name: str, stdout: str) -> tuple[int, dict[str, str], dict[str, object]]:
    response_headers, body = _split_final_response(stdout)
    lines = response_headers.splitlines()
    try:
        protocol, status, *_ = lines[0].split()
        version_parts = protocol.removeprefix("HTTP/").split(".")
        valid_version = (
            len(version_parts) == 2
            and version_parts[0] == "1"
            and len(version_parts[1]) == 1
            and version_parts[1].isascii()
            and version_parts[1].isdigit()
        ) or protocol in {"HTTP/2", "HTTP/3"}
        valid_status = len(status) == 3 and status.isascii() and status.isdigit()
        if not valid_version or not valid_status:
            raise ValueError
        actual_status = int(status)
    except (IndexError, ValueError) as error:
        raise RuntimeError(f"{name}: response carried no HTTP status") from error
    headers = {
        line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
        for line in lines[1:]
        if ":" in line
    }
    try:
        response = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{name}: invalid JSON response") from error
    if not isinstance(response, dict):
        raise RuntimeError(f"{name}: response was not a JSON object")
    return actual_status, headers, response


def _header_matches(actual: str | None, expected: str, header: str) -> bool:
    """Compare a header value; content-type matches on media type, the rest exactly.

    Accepting parameters such as ``application/json; charset=utf-8`` without
    failing the envelope is why content-type gets the partitioned comparison.
    """
    if header == _CONTENT_TYPE_HEADER_NAME and actual is not None:
        return (
            actual.partition(";")[0].strip().lower()
            == expected.partition(";")[0].strip().lower()
        )
    return actual == expected


def _headers_match(actual_headers: object, expected_headers: object) -> bool:
    """Whether the API envelope's ``headers`` object equals the expected one.

    Header names compare case-insensitively, and the content-type value by media
    type, so a parameter on either side does not fail the match.
    """
    matches = actual_headers == expected_headers
    if isinstance(actual_headers, dict) and isinstance(expected_headers, dict):
        actual_normalized = {header.lower(): value for header, value in actual_headers.items()}
        expected_normalized = {header.lower(): value for header, value in expected_headers.items()}
        matches = actual_normalized == expected_normalized
        if (
            actual_normalized.keys() == expected_normalized.keys()
            and _CONTENT_TYPE_HEADER_NAME in actual_normalized
        ):
            actual_content_type = actual_normalized[_CONTENT_TYPE_HEADER_NAME]
            expected_content_type = expected_normalized[_CONTENT_TYPE_HEADER_NAME]
            if isinstance(actual_content_type, str) and isinstance(expected_content_type, str):
                matches = (
                    actual_content_type.partition(";")[0].strip().lower()
                    == expected_content_type.partition(";")[0].strip().lower()
                    and all(
                        actual_normalized[header] == expected_normalized[header]
                        for header in actual_normalized.keys() - {_CONTENT_TYPE_HEADER_NAME}
                    )
                )
    return matches


def _verify_outer_headers(
    name: str,
    actual_status: int,
    headers: dict[str, str],
    expectation: HttpFunctionExpectation,
) -> None:
    if actual_status != expectation.status:
        raise RuntimeError(f"{name}: HTTP status was {actual_status}, expected {expectation.status}")
    for header, expected_value in expectation.required_headers:
        actual_value = headers.get(header.lower())
        if actual_value is None:
            raise RuntimeError(f"{name}: missing required header {header}")
        if not _header_matches(actual_value, expected_value, header.lower()):
            raise RuntimeError(
                f"{name}: required header {header} was {actual_value!r}, "
                f"expected {expected_value!r}"
            )
    for header in expectation.forbidden_headers:
        if header.lower() in headers:
            raise RuntimeError(f"{name}: forbidden header {header} was present")
    for header, forbidden_value in expectation.forbidden_header_values:
        actual_value = headers.get(header.lower())
        if _header_matches(actual_value, forbidden_value, header.lower()):
            raise RuntimeError(f"{name}: forbidden header {header} had value {forbidden_value!r}")


def _verify_api_fields(
    name: str, response: dict[str, object], expectation: HttpFunctionExpectation
) -> None:
    """Assert the envelope's status/output/statusCode/encoding fields."""
    for field, expected in (
        ("status", expectation.api_status),
        ("output", expectation.output),
        ("statusCode", expectation.status_code),
        ("encoding", expectation.encoding),
    ):
        if expected is not _UNSET and response.get(field) != expected:
            raise RuntimeError(f"{name}: {field} was {response.get(field)!r}, expected {expected!r}")


def _verify_api_envelope(
    name: str, response: dict[str, object], expectation: HttpFunctionExpectation
) -> None:
    _verify_api_fields(name, response, expectation)
    if expectation.api_headers is _UNSET:
        return
    if not _headers_match(response.get("headers"), expectation.api_headers):
        raise RuntimeError(f"{name}: headers was {response.get('headers')!r}, expected {expectation.api_headers!r}")


def _verify_decoded_output(
    name: str, response: dict[str, object], expectation: HttpFunctionExpectation
) -> None:
    if expectation.decoded_bytes is None and expectation.decoded_prefix is None:
        return
    output = response.get("output")
    if not isinstance(output, str):
        raise RuntimeError(f"{name}: base64 output was not a string")
    try:
        decoded = base64.b64decode(output, validate=True)
    except ValueError as error:
        raise RuntimeError(f"{name}: invalid base64 output") from error
    if expectation.decoded_bytes is not None and decoded != expectation.decoded_bytes:
        raise RuntimeError(f"{name}: decoded output was {decoded!r}, expected {expectation.decoded_bytes!r}")
    if expectation.decoded_prefix is not None and not decoded.startswith(expectation.decoded_prefix):
        raise RuntimeError(
            f"{name}: decoded output did not start with {expectation.decoded_prefix!r}"
        )


class HttpFunctionContractTask(CommandTask):
    """Invoke a function and assert its complete HTTP envelope."""

    def __init__(
        self,
        name: str,
        *,
        payload: str,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        expectation: HttpFunctionExpectation,
        headers: tuple[str, ...] = (),
        content_type: str = "application/json",
        cwd: Path | None = None,
    ) -> None:
        def verify(result: TaskResult) -> None:
            contract: tuple[int, dict[str, str], dict[str, object]] = _parse_contract_response(
                name, result.stdout
            )
            actual_status, response_headers, response = contract
            _verify_outer_headers(name, actual_status, response_headers, expectation)
            _verify_api_envelope(name, response, expectation)
            _verify_decoded_output(name, response, expectation)

        request_headers = tuple(part for header in headers for part in ("-H", header))
        super().__init__(
            title=f"Verify {name} HTTP envelope",
            argv=_argv(
                endpoint,
                lambda base: (
                    "curl",
                    "-isS",
                    "-H",
                    f"Content-Type: {content_type}",
                    *request_headers,
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
            verify_invocation(cast(TaskResult, replace(result, stdout=body)))

        super().__init__(
            title=f"Invoke {name}",
            argv=_argv(
                endpoint,
                lambda base: (
                    "curl",
                    "-fsS" if require_header is None else "-isS",
                    "-H",
                    _JSON_CONTENT_TYPE_HEADER,
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

        data = ("-H", _JSON_CONTENT_TYPE_HEADER, "--data", payload) if payload else ()
        super().__init__(
            title=title or f"Expect {expected_status} from {url}",
            argv=("curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", *data, url),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=check,
        )
