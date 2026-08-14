from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import Resource, TaskInputs
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.cli_function import CliFunctionInvokeTask
from sonata_tasks.http_function import HttpFunctionContractTask, HttpFunctionExpectation, HttpFunctionInvokeTask
from sonata_tasks.invocation import verify_invocation

SUCCESS = '{"status":"success","output":"5 words"}'


@dataclass
class RecordingExecutor:
    stdout: str = SUCCESS
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=self.stdout)


def _result(stdout: str) -> TaskResult:
    return TaskResult(task_id="", status="passed", return_code=0, stdout=stdout)


def test_a_successful_response_passes() -> None:
    verify_invocation(_result(SUCCESS))


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("<html>502</html>", "was not JSON"),
        ('["success"]', "was not JSON object"),
        ('{"status":"error","output":""}', "did not report success"),
        ('{"status":"success"}', "carried no output"),
    ],
)
def test_it_separates_the_ways_an_invocation_can_be_unusable(stdout: str, expected: str) -> None:
    """The shell version piped this through two `grep -q` calls, which reported
    every one of these four cases identically."""
    with pytest.raises(RuntimeError, match=expected):
        verify_invocation(_result(stdout))


def test_http_invoke_posts_the_payload_to_the_invoke_endpoint() -> None:
    executor = RecordingExecutor()

    task = HttpFunctionInvokeTask(
        "word-stats",
        payload='{"text":"a b"}',
        endpoint="http://127.0.0.1:18080",
        executor=executor,
        role="host",
    )
    _ = task.run(TaskInputs.empty())

    assert task.title == "Invoke word-stats"
    assert executor.seen[0].argv == (
        "curl",
        "-fsS",
        "-H",
        "Content-Type: application/json",
        "--data",
        '{"text":"a b"}',
        "http://127.0.0.1:18080/v1/functions/word-stats:invoke",
    )


def test_the_endpoint_can_arrive_as_a_resource_value() -> None:
    """On Kubernetes the address exists only once the Service does, so it is read
    at run time from the resource that created it rather than spelled up front."""
    executor = RecordingExecutor()
    endpoint: Resource[str] = Resource(
        title="Acquire control plane",
        acquire=lambda _inputs: "http://10.43.0.7:8080",
        release=lambda _inputs, _value: None,
    )
    task = HttpFunctionInvokeTask(
        "word-stats",
        payload="{}",
        endpoint=endpoint,
        executor=executor,
        role="stack",
    )

    _ = task.run(TaskInputs._for_resources({endpoint: "http://10.43.0.7:8080"}, {endpoint}))

    assert executor.seen[0].argv[-1] == (
        "http://10.43.0.7:8080/v1/functions/word-stats:invoke"
    )


def test_cli_invoke_drives_the_binary_instead_of_curl() -> None:
    executor = RecordingExecutor()

    _ = CliFunctionInvokeTask(
        "word-stats",
        payload='{"text":"a b"}',
        cli_argv=("nanofaas", "--endpoint", "http://cp:8080"),
        executor=executor,
        role="stack",
    ).run(TaskInputs.empty())

    assert executor.seen[0].argv == (
        "nanofaas",
        "--endpoint",
        "http://cp:8080",
        "invoke",
        "word-stats",
        "--data",
        '{"text":"a b"}',
    )


@pytest.mark.parametrize("transport", ["http", "cli"])
def test_both_transports_reject_a_200_that_carries_an_error(transport: str) -> None:
    """Curl exits 0 here: without the shared check, both would call it a pass."""
    executor = RecordingExecutor(stdout='{"status":"error","output":"boom"}')
    task = (
        HttpFunctionInvokeTask(
            "fn", payload="{}", endpoint="http://cp:8080", executor=executor, role="host"
        )
        if transport == "http"
        else CliFunctionInvokeTask(
            "fn", payload="{}", cli_argv=("nanofaas",), executor=executor, role="host"
        )
    )

    with pytest.raises(RuntimeError, match="did not report success"):
        task.run(TaskInputs.empty())


def test_it_repeats_the_reason_the_control_plane_gave() -> None:
    """A live run once reported only "error", leaving the reader to guess between
    a function that threw, a pod that was not ready, and a dispatch that found no
    endpoint. The answer was in the response all along."""
    with pytest.raises(RuntimeError, match=r"DISPATCH_FAILED: no ready endpoint"):
        verify_invocation(
            _result(
                '{"status":"error","error":{"code":"DISPATCH_FAILED",'
                '"message":"no ready endpoint"}}'
            )
        )


@pytest.mark.parametrize(
    "error",
    ['"boom"', "null", "{}", '{"code":null,"message":null}'],
)
def test_a_response_without_a_usable_reason_still_reports_the_status(error: str) -> None:
    with pytest.raises(RuntimeError, match=r"did not report success: 'error'$"):
        verify_invocation(_result('{"status":"error","error":' + error + "}"))


def test_it_reports_whichever_half_of_the_reason_exists() -> None:
    with pytest.raises(RuntimeError, match=r"\(no ready endpoint\)"):
        verify_invocation(
            _result('{"status":"error","error":{"message":"no ready endpoint"}}')
        )


ENVELOPE_422 = (
    "HTTP/1.1 422 Unprocessable Content\r\n"
    "Content-Type: application/json\r\n"
    "X-NanoFaaS-Function-Status: true\r\n"
    "X-Caller-Id: real\r\n"
    "\r\n"
    '{"status":"success","output":{"header":"real","body":"unique"},"statusCode":422}'
)


def _contract(
    stdout: str = ENVELOPE_422, *, expectation: HttpFunctionExpectation | None = None
) -> tuple[HttpFunctionContractTask, RecordingExecutor]:
    executor = RecordingExecutor(stdout=stdout)
    task = HttpFunctionContractTask(
        "envelope-probe",
        payload='{"message":"body-sentinel"}',
        endpoint="http://cp:8080",
        executor=executor,
        role="host",
        headers=("X-Caller-Id: preserved",),
        expectation=expectation
        or HttpFunctionExpectation(
            status=422,
            api_status="success",
            output={"header": "real", "body": "unique"},
            status_code=422,
            required_headers=(("x-nanofaas-function-status", "true"), ("x-caller-id", "real")),
            forbidden_headers=("x-secret",),
        ),
    )
    return task, executor


def test_http_contract_accepts_an_exact_422_function_envelope() -> None:
    task, executor = _contract("HTTP/1.1 100 Continue\r\n\r\n" + ENVELOPE_422)

    _ = task.run(TaskInputs.empty())

    assert executor.seen[0].argv == (
        "curl",
        "-isS",
        "-H",
        "Content-Type: application/json",
        "-H",
        "X-Caller-Id: preserved",
        "--data",
        '{"message":"body-sentinel"}',
        "http://cp:8080/v1/functions/envelope-probe:invoke",
    )


def test_http_contract_rejects_a_malformed_status_line() -> None:
    task, _ = _contract(ENVELOPE_422.replace("HTTP/1.1 422", "not-http"))

    with pytest.raises(RuntimeError, match="response carried no HTTP status"):
        task.run(TaskInputs.empty())


def test_http_contract_rejects_a_numeric_status_with_a_non_http_protocol() -> None:
    task, _ = _contract(ENVELOPE_422.replace("HTTP/1.1 422", "NOT-HTTP 422"))

    with pytest.raises(RuntimeError, match="response carried no HTTP status"):
        task.run(TaskInputs.empty())


def test_http_contract_rejects_http_1_without_a_minor_version() -> None:
    task, _ = _contract(ENVELOPE_422.replace("HTTP/1.1 422", "HTTP/1 422"))

    with pytest.raises(RuntimeError, match="response carried no HTTP status"):
        task.run(TaskInputs.empty())


@pytest.mark.parametrize("protocol", ["HTTP/2", "HTTP/3"])
def test_http_contract_accepts_curl_modern_http_status_lines(protocol: str) -> None:
    task, _ = _contract(ENVELOPE_422.replace("HTTP/1.1 422", f"{protocol} 422"))

    _ = task.run(TaskInputs.empty())


def test_http_contract_rejects_a_mismatched_outer_header_value() -> None:
    task, _ = _contract(ENVELOPE_422.replace("X-Caller-Id: real", "X-Caller-Id: spoofed"))

    with pytest.raises(RuntimeError, match="required header x-caller-id was 'spoofed'"):
        task.run(TaskInputs.empty())


@pytest.mark.parametrize(
    ("stdout", "expectation", "error"),
    [
        (ENVELOPE_422.replace("422 Unprocessable Content", "200 OK"), None, "status was 200"),
        (ENVELOPE_422.replace("X-NanoFaaS-Function-Status: true\r\n", ""), None, "missing required header"),
        (ENVELOPE_422.replace("unique", "wrong-output"), None, "output was"),
        (ENVELOPE_422.replace("X-Caller-Id: real\r\n", "X-Caller-Id: real\r\nX-Secret: nope\r\n"), None, "forbidden header"),
        (
            "HTTP/1.1 200 OK\r\n\r\n"
            '{"status":"success","output":"not base64!","statusCode":200}',
            HttpFunctionExpectation(
                status=200,
                api_status="success",
                output="not base64!",
                status_code=200,
                decoded_bytes=b"expected",
            ),
            "invalid base64 output",
        ),
    ],
)
def test_http_contract_rejects_a_mismatched_envelope(
    stdout: str, expectation: HttpFunctionExpectation | None, error: str
) -> None:
    task, _ = _contract(stdout, expectation=expectation)

    with pytest.raises(RuntimeError, match=error):
        task.run(TaskInputs.empty())


def test_http_contract_accepts_a_plain_response_when_no_markers_are_expected() -> None:
    task, _ = _contract(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
        '{"status":"success","output":"plain","statusCode":200}',
        expectation=HttpFunctionExpectation(
            status=200, api_status="success", output="plain", status_code=200
        ),
    )

    _ = task.run(TaskInputs.empty())


@pytest.mark.parametrize(
    ("marker", "error"),
    [
        ('"statusCode":200', "statusCode was 200"),
        ('"headers":{"Content-Type":"text/plain"}', "headers was"),
        ('"encoding":"utf-8"', "encoding was 'utf-8'"),
    ],
)
def test_http_contract_rejects_api_markers_expected_to_be_null(
    marker: str, error: str
) -> None:
    task, _ = _contract(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
        f'{{"status":"success","output":"plain",{marker}}}',
        expectation=HttpFunctionExpectation(
            status=200,
            api_status="success",
            output="plain",
            status_code=None,
            api_headers=None,
            encoding=None,
        ),
    )

    with pytest.raises(RuntimeError, match=error):
        task.run(TaskInputs.empty())


def test_success_only_invoke_keeps_its_first_header_block_behavior() -> None:
    task = HttpFunctionInvokeTask(
        "word-stats",
        payload="{}",
        endpoint="http://cp:8080",
        executor=RecordingExecutor(
            stdout=(
                "HTTP/1.1 100 Continue\r\n\r\n"
                "HTTP/1.1 200 OK\r\nX-Final: true\r\n\r\n"
                '{"status":"success","output":"ok"}'
            )
        ),
        role="host",
        require_header="X-Final",
    )

    with pytest.raises(RuntimeError, match="no X-Final header"):
        task.run(TaskInputs.empty())


def test_http_contract_decodes_expected_base64_bytes() -> None:
    task, _ = _contract(
        "HTTP/1.1 200 OK\r\n\r\n"
        '{"status":"success","output":"cGF5bG9hZA==","statusCode":200}',
        expectation=HttpFunctionExpectation(
            status=200,
            api_status="success",
            output="cGF5bG9hZA==",
            status_code=200,
            decoded_bytes=b"payload",
        ),
    )

    _ = task.run(TaskInputs.empty())


def test_http_contract_rejects_different_decoded_base64_bytes() -> None:
    task, _ = _contract(
        "HTTP/1.1 200 OK\r\n\r\n"
        '{"status":"success","output":"cGF5bG9hZA==","statusCode":200}',
        expectation=HttpFunctionExpectation(
            status=200,
            api_status="success",
            output="cGF5bG9hZA==",
            status_code=200,
            decoded_bytes=b"different",
        ),
    )

    with pytest.raises(RuntimeError, match="decoded output was"):
        task.run(TaskInputs.empty())


def test_http_contract_accepts_a_base64_output_with_the_expected_png_signature() -> None:
    task, _ = _contract(
        "HTTP/1.1 200 OK\r\n\r\n"
        '{"status":"success","output":"iVBORw0KGgpwYXlsb2Fk","statusCode":200,'
        '"headers":{"Content-Type":"image/png"},"encoding":"base64"}',
        expectation=HttpFunctionExpectation(
            status=200,
            api_status="success",
            output="iVBORw0KGgpwYXlsb2Fk",
            status_code=200,
            api_headers={"Content-Type": "image/png"},
            encoding="base64",
            decoded_prefix=b"\x89PNG\r\n\x1a\n",
        ),
    )

    _ = task.run(TaskInputs.empty())


def test_http_contract_matches_api_header_names_case_insensitively() -> None:
    task, _ = _contract(
        "HTTP/1.1 200 OK\r\n\r\n"
        '{"status":"success","output":"iVBORw0KGgpwYXlsb2Fk","statusCode":200,'
        '"headers":{"Content-type":"image/png; charset=binary"},"encoding":"base64"}',
        expectation=HttpFunctionExpectation(
            status=200,
            api_headers={"Content-Type": "image/png"},
            encoding="base64",
        ),
    )

    _ = task.run(TaskInputs.empty())


def test_http_contract_rejects_a_base64_output_with_the_wrong_signature() -> None:
    task, _ = _contract(
        "HTTP/1.1 200 OK\r\n\r\n"
        '{"status":"success","output":"iVBORw0KGgpwYXlsb2Fk","statusCode":200}',
        expectation=HttpFunctionExpectation(
            status=200,
            api_status="success",
            output="iVBORw0KGgpwYXlsb2Fk",
            status_code=200,
            decoded_prefix=b"GIF89a",
        ),
    )

    with pytest.raises(RuntimeError, match="did not start with"):
        task.run(TaskInputs.empty())


@pytest.mark.parametrize(
    ("response", "expectation", "message"),
    [
        (
            '{"status":"success","output":"ok","statusCode":200}',
            HttpFunctionExpectation(status=200, api_headers={"Content-Type": "image/png"}),
            "headers was None",
        ),
        (
            '{"status":"success","output":"ok","statusCode":200,"encoding":"json"}',
            HttpFunctionExpectation(status=200, encoding="base64"),
            "encoding was 'json'",
        ),
    ],
)
def test_http_contract_rejects_mismatched_api_envelope_fields(
    response: str, expectation: HttpFunctionExpectation, message: str
) -> None:
    task, _ = _contract("HTTP/1.1 200 OK\r\n\r\n" + response, expectation=expectation)

    with pytest.raises(RuntimeError, match=message):
        task.run(TaskInputs.empty())


def test_http_contract_requires_encoding_marker_but_rejects_function_content_type_on_outer_response() -> None:
    task, _ = _contract(
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: image/png; charset=binary\r\n"
        "X-NanoFaaS-Encoding: base64\r\n\r\n"
        '{"status":"success","output":"ok","statusCode":200}',
        expectation=HttpFunctionExpectation(
            status=200,
            required_headers=(("X-NanoFaaS-Encoding", "base64"),),
            forbidden_header_values=(("Content-Type", "image/png"),),
        ),
    )

    with pytest.raises(RuntimeError, match="forbidden header Content-Type"):
        task.run(TaskInputs.empty())


def test_http_contract_rejects_missing_required_encoding_marker() -> None:
    task, _ = _contract(
        "HTTP/1.1 200 OK\r\n\r\n"
        '{"status":"success","output":"ok","statusCode":200}',
        expectation=HttpFunctionExpectation(
            status=200, required_headers=(("X-NanoFaaS-Encoding", "base64"),)
        ),
    )

    with pytest.raises(RuntimeError, match="missing required header X-NanoFaaS-Encoding"):
        task.run(TaskInputs.empty())


def test_http_contract_accepts_parameters_on_the_outer_json_content_type() -> None:
    task, _ = _contract(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n\r\n"
        '{"status":"success","output":"ok","statusCode":200}',
        expectation=HttpFunctionExpectation(
            status=200, required_headers=(("Content-Type", "application/json"),)
        ),
    )

    _ = task.run(TaskInputs.empty())
