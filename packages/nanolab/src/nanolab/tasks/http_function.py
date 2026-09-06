from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, replace
import json
from pathlib import Path
from time import monotonic, sleep
from typing import cast

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.bindings import CommandTaskExecutor
from nanolab.tasks.execution import ExecutionRole
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import Argv, CommandTask
from sonata_tasks.core.fingerprint import fingerprint_digest
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.http import Endpoint, HttpStatusCheckTask as SharedHttpStatusCheckTask
from sonata_tasks.http import endpoint_argv
from nanolab.tasks.invocation import verify_invocation
from nanolab.tasks.manifest import FunctionManifest

# Reaching the control plane over HTTP, for workflows that talk to its API rather
# than drive the nanofaas CLI. `role` is required throughout: curling from the
# host and curling from inside a VM reach different network namespaces, and the
# endpoint that works in one is usually wrong in the other.

_JSON_CONTENT_TYPE_HEADER = "Content-Type: application/json"
_CONTENT_TYPE_HEADER_NAME = "content-type"


def _argv(endpoint: Endpoint, build: Callable[[str], tuple[str, ...]]) -> Argv:
    return endpoint_argv(endpoint, build)


def _endpoint_identity(endpoint: Endpoint) -> dict[str, str]:
    if isinstance(endpoint, str):
        return {"kind": "static", "url": endpoint}
    return {"kind": "resource", "title": endpoint.title}


def _semantic_key(namespace: str, **payload: object) -> str:
    """Hash captured configuration so journals contain no request secrets."""
    return f"{namespace}:{fingerprint_digest(payload)}"


def _task_fingerprint(namespace: str, **payload: object) -> object:
    return {
        "schema": 1,
        "digest": fingerprint_digest({"namespace": namespace, **payload}),
    }



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


def _registration_matches_manifest(manifest: FunctionManifest, response: object) -> bool:
    """Whether an existing registration is the same function this call would create.

    Only the fields the manifest itself controls: enough to tell "this is the
    function we were about to register, already there" from "something else is
    using this name," without demanding the deployment-derived fields
    (`deploymentBackend`, `endpointUrl`) that `_verify_registration_response`
    checks on a fresh registration - those can legitimately differ after a
    reconcile.
    """
    if not isinstance(response, dict):
        return False
    return (
        response.get("name") == manifest.name
        and response.get("image") == manifest.image
        and response.get("requestedExecutionMode") == manifest.execution_mode
    )


class HttpFunctionRegisterTask(CommandTask):
    """POST a function manifest to the control plane.

    A 409 is not automatically a failure: the control plane restores its
    persisted catalog on every restart, reconciling each managed function
    against its real backend before serving traffic again
    (`FunctionCatalogRestorer`). A workflow that registers, runs a load test,
    and releases has no way to know the control plane restarted in between -
    only that the function it is about to register already exists. If a GET
    shows that existing registration is this same function (same name, image,
    execution mode), the 409 means the earlier registration already succeeded
    durably and survived the restart; that is success, not conflict. A 409 for
    a genuinely different function under this name still fails, same as before.
    """

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

        self._manifest = manifest
        self._endpoint = endpoint
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
                    # Without this, `--retry` ignores a refused connection and the
                    # whole retry budget is never spent: curl treats ECONNREFUSED
                    # as a hard failure. A control plane that is rolling — which
                    # it is on every cell of a build-comparison matrix — refuses
                    # for the second or two between the pod becoming Ready and
                    # kube-proxy programming the NodePort. Measured: registration
                    # failed with exit 7 after 0.5s while holding a 30s budget,
                    # having retried nothing. `compose.py` already carries the
                    # flag for the same reason.
                    "--retry-connrefused",
                    "-H",
                    _JSON_CONTENT_TYPE_HEADER,
                    "--data",
                    manifest.json(),
                    f"{base}/v1/functions",
                ),
            ),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd, expected_exit_codes=frozenset({0, 22})),
            semantic_key=_semantic_key(
                "nanolab.http-function.register:v2",
                endpoint=_endpoint_identity(endpoint),
                manifest=manifest.body(),
                expected_backend=expected_backend,
                expected_endpoint_prefix=expected_endpoint_prefix,
            ),
            verify=verify if expected_backend is not None or expected_endpoint_prefix is not None else None,
        )

    def run(self, inputs: TaskInputs) -> TaskOutcome[TaskResult]:
        result = self.executor.run(self._spec(inputs))
        if result.return_code == 0:
            if self.verify is not None:
                self.verify(result)
            return TaskOutcome(value=result)
        if result.return_code == 22:
            base = inputs.resource(self._endpoint) if not isinstance(self._endpoint, str) else self._endpoint
            existing = self.executor.run(
                CommandTaskSpec(
                    task_id="",
                    summary=f"Check existing registration for {self._manifest.name}",
                    argv=("curl", "-fsS", f"{base}/v1/functions/{self._manifest.name}"),
                    role=self.role,
                    options=replace(
                        self.options,
                        expected_exit_codes=frozenset({0}),
                    ),
                )
            )
            if existing.status == "passed":
                try:
                    response = json.loads(existing.stdout)
                except json.JSONDecodeError:
                    response = None
                if _registration_matches_manifest(self._manifest, response):
                    return TaskOutcome(value=result)
        detail = "\n".join(part for part in (result.stderr.strip(), result.stdout.strip()) if part)
        detail = detail or "no output"
        raise RuntimeError(f"{self.title} failed (exit {result.return_code}): {detail}")


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
            options=CommandOptions(cwd=cwd, expected_exit_codes=frozenset({0, 7, 22})),
            semantic_key=_semantic_key(
                "nanolab.http-function.delete:v2",
                endpoint=_endpoint_identity(endpoint),
                name=name,
            ),
        )


class HttpFunctionSetReplicasTask(CommandTask):
    """Set the desired replica target for one managed function."""

    def __init__(
        self,
        name: str,
        *,
        replicas: int,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            title=f"Set {name} replicas to {replicas}",
            argv=_argv(
                endpoint,
                lambda base: (
                    "curl",
                    "-fsS",
                    "-X",
                    "PUT",
                    "-H",
                    _JSON_CONTENT_TYPE_HEADER,
                    "--data",
                    json.dumps({"replicas": replicas}, separators=(",", ":")),
                    f"{base}/v1/functions/{name}/replicas",
                ),
            ),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
            semantic_key=_semantic_key(
                "nanolab.http-function.set-replicas:v2",
                endpoint=_endpoint_identity(endpoint),
                name=name,
                replicas=replicas,
            ),
        )


class HttpFunctionBackendTask(CommandTask):
    """Read a function registration and require its managed backend."""

    def __init__(
        self,
        name: str,
        *,
        backend: str,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        cwd: Path | None = None,
    ) -> None:
        def verify(result: TaskResult) -> None:
            try:
                response = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"{name}: invalid function response") from error
            if response.get("deploymentBackend") != backend:
                raise RuntimeError(
                    f"{name}: deploymentBackend was {response.get('deploymentBackend')!r}, "
                    f"expected {backend!r}"
                )

        super().__init__(
            title=f"Verify {name} backend",
            argv=_argv(
                endpoint,
                lambda base: ("curl", "-fsS", f"{base}/v1/functions/{name}"),
            ),
            executor=executor,
            role=role,
            options=CommandOptions(cwd=cwd),
            semantic_key=_semantic_key(
                "nanolab.http-function.backend:v2",
                endpoint=_endpoint_identity(endpoint),
                name=name,
                backend=backend,
            ),
            verify=verify,
        )


class HttpFunctionReplicaStatusTask(Task[None]):
    """Wait until a managed function reaches its requested replica target."""

    def __init__(
        self,
        name: str,
        *,
        replicas: int,
        endpoint: Endpoint,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        timeout_seconds: float = 30,
        poll_seconds: float = 0.5,
        clock: Callable[[], float] = monotonic,
        sleep_fn: Callable[[float], None] = sleep,
        cwd: Path | None = None,
    ) -> None:
        self.title = f"Wait for {name} replicas"
        self._name = name
        self._replicas = replicas
        self._endpoint = endpoint
        self._executor = executor
        self._role: ExecutionRole = role
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._sleep = sleep_fn
        self._cwd = cwd
        self._fingerprint = _task_fingerprint(
            "nanolab.http-function.replica-status:v2",
            endpoint=_endpoint_identity(endpoint),
            name=name,
            replicas=replicas,
            role=role,
            binding_key=executor.binding_key(role),
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            cwd=cwd,
        )

    def _fingerprint_payload(self) -> object:
        return self._fingerprint

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        command = CommandTask(
                      title=self.title,
                      argv=_argv(
                self._endpoint,
                lambda base: ("curl", "-fsS", f"{base}/v1/functions/{self._name}/replicas"),
            ),
                      executor=self._executor,
                      role=self._role,
                      options=CommandOptions(cwd=self._cwd),
                      semantic_key=_semantic_key(
                          "nanolab.http-function.replica-status:v2",
                          endpoint=_endpoint_identity(self._endpoint),
                          name=self._name,
                          replicas=self._replicas,
                      ),
                  )
        deadline = self._clock() + self._timeout_seconds
        last_response: object = None
        while True:
            result = _command_result(command.run(inputs), self.title)
            try:
                response = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"{self._name}: invalid replica status") from error
            last_response = response
            if (
                response.get("desiredReplicas") == self._replicas
                and response.get("readyReplicas") == self._replicas
            ):
                return TaskOutcome(value=None)
            if self._clock() >= deadline:
                raise RuntimeError(f"{self._name}: replicas did not become ready; last response {last_response!r}")
            self._sleep(self._poll_seconds)


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
        self._fingerprint = _task_fingerprint(
            "nanolab.http-function.enqueue-task:v2",
            endpoint=_endpoint_identity(endpoint),
            name=name,
            payload=payload,
            idempotency_key=idempotency_key,
            match_upstream=match_upstream,
            role=role,
            binding_key=executor.binding_key(role),
            cwd=cwd,
        )
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
                            options=CommandOptions(cwd=cwd),
                            semantic_key=_semantic_key(
                                "nanolab.http-function.enqueue:v2",
                                endpoint=_endpoint_identity(endpoint),
                                name=name,
                                payload=payload,
                                idempotency_key=idempotency_key,
                                match_upstream=match_upstream,
                            ),
                        )

    def _fingerprint_payload(self) -> object:
        return self._fingerprint

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
        self._fingerprint = _task_fingerprint(
            "nanolab.http-function.execution-success:v2",
            endpoint=_endpoint_identity(endpoint),
            role=role,
            binding_key=executor.binding_key(role),
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            expected_output=("unset" if expected_output is _UNSET else expected_output),
            expected_status_code=(
                "unset" if expected_status_code is _UNSET else expected_status_code
            ),
            cwd=cwd,
        )

    def _fingerprint_payload(self) -> object:
        return self._fingerprint

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:  # NOSONAR (S3776): polling state machine reports precise failures
        execution_id = inputs.upstream()
        if not isinstance(execution_id, str) or not execution_id:
            raise RuntimeError(f"{self.title}: expected an execution id from enqueue")
        command = CommandTask(
                      title=self.title,
                      argv=_argv(self._endpoint, lambda base: ("curl", "-fsS", f"{base}/v1/executions/{execution_id}")),
                      executor=self._executor,
                      role=self._role,
                      options=CommandOptions(cwd=self._cwd),
                      semantic_key=_semantic_key(
                          "nanolab.http-function.execution-success:v2",
                          endpoint=_endpoint_identity(self._endpoint),
                          execution_id=execution_id,
                          expected_output=(
                              "unset" if self._expected_output is _UNSET else self._expected_output
                          ),
                          expected_status_code=(
                              "unset"
                              if self._expected_status_code is _UNSET
                              else self._expected_status_code
                          ),
                      ),
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
            options=CommandOptions(cwd=cwd),
            semantic_key=_semantic_key(
                "nanolab.http-function.contract:v2",
                endpoint=_endpoint_identity(endpoint),
                name=name,
                payload=payload,
                headers=headers,
                content_type=content_type,
                expectation={
                    "status": expectation.status,
                    "api_status": "unset" if expectation.api_status is _UNSET else expectation.api_status,
                    "output": "unset" if expectation.output is _UNSET else expectation.output,
                    "status_code": (
                        "unset" if expectation.status_code is _UNSET else expectation.status_code
                    ),
                    "api_headers": (
                        "unset" if expectation.api_headers is _UNSET else expectation.api_headers
                    ),
                    "encoding": "unset" if expectation.encoding is _UNSET else expectation.encoding,
                    "required_headers": expectation.required_headers,
                    "forbidden_headers": expectation.forbidden_headers,
                    "forbidden_header_values": expectation.forbidden_header_values,
                    "decoded_bytes": expectation.decoded_bytes,
                    "decoded_prefix": expectation.decoded_prefix,
                },
            ),
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
            options=CommandOptions(cwd=cwd),
            semantic_key=_semantic_key(
                "nanolab.http-function.invoke:v2",
                endpoint=_endpoint_identity(endpoint),
                name=name,
                payload=payload,
                require_header=require_header,
            ),
            verify=verify,
        )


class HttpStatusCheckTask(SharedHttpStatusCheckTask):
    """nanoFaaS status check that uses JSON for invocation payloads."""
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
        super().__init__(
            url=url, expected_status=expected_status, executor=executor, role=role,
            payload=payload,
            headers={"Content-Type": "application/json"} if payload is not None else {},
            options=CommandOptions(cwd=cwd), title=title,
        )
