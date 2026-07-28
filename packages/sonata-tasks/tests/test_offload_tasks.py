from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.http_function import HttpFunctionInvokeTask, HttpStatusCheckTask
from sonata_tasks.manifest import FunctionManifest
from sonata_tasks.metrics import PrometheusScrapeCheckTask

OFFLOADED = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: application/json\r\n"
    "X-NanoFaaS-Offloaded: true\r\n"
    "\r\n"
    '{"status":"success","output":{"words":2}}'
)
LOCAL = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: application/json\r\n"
    "\r\n"
    '{"status":"success","output":{"words":2}}'
)


@dataclass
class ScriptedExecutor:
    stdout: str = ""
    seen: list[CommandTaskSpec] = field(default_factory=list[CommandTaskSpec])

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=self.stdout)


def test_the_manifest_carries_an_offload_policy_when_there_is_one() -> None:
    """The edge registers the same function as LOCAL with a policy; without this
    field the body could not say so."""
    body = FunctionManifest(
        name="word-stats", image="img", execution_mode="LOCAL", offload={"mode": "always"}
    ).body()

    assert body["offload"] == {"mode": "always"}
    assert body["executionMode"] == "LOCAL"


def test_a_manifest_without_a_policy_omits_the_field() -> None:
    assert "offload" not in FunctionManifest(name="word-stats", image="img").body()


def test_an_offloaded_invocation_passes_and_reads_the_body_behind_the_headers() -> None:
    executor = ScriptedExecutor(stdout=OFFLOADED)

    task = HttpFunctionInvokeTask(
        "word-stats",
        payload="{}",
        endpoint="http://edge:18080",
        executor=executor,
        role="host",
        require_header="X-NanoFaaS-Offloaded",
    )
    _ = task.run(TaskInputs.empty())

    # -i, not -f: the headers have to arrive in the same stdout as the body.
    assert "-isS" in executor.seen[0].argv


def test_an_answer_computed_locally_fails_even_though_it_is_a_valid_response() -> None:
    """The point of the invocation is that it was proxied. A perfectly good
    local answer is the failure this catches."""
    task = HttpFunctionInvokeTask(
        "word-stats",
        payload="{}",
        endpoint="http://edge:18080",
        executor=ScriptedExecutor(stdout=LOCAL),
        role="host",
        require_header="X-NanoFaaS-Offloaded",
    )

    with pytest.raises(RuntimeError, match="no X-NanoFaaS-Offloaded header"):
        task.run(TaskInputs.empty())


def test_the_body_is_still_verified_behind_the_headers() -> None:
    offloaded_but_failed = OFFLOADED.replace(
        '{"status":"success","output":{"words":2}}', '{"status":"error"}'
    )
    task = HttpFunctionInvokeTask(
        "word-stats",
        payload="{}",
        endpoint="http://edge:18080",
        executor=ScriptedExecutor(stdout=offloaded_but_failed),
        role="host",
        require_header="X-NanoFaaS-Offloaded",
    )

    with pytest.raises(RuntimeError, match="did not report success"):
        task.run(TaskInputs.empty())


def test_an_invocation_that_asks_for_no_header_is_unchanged() -> None:
    executor = ScriptedExecutor(stdout='{"status":"success","output":1}')

    _ = HttpFunctionInvokeTask(
        "word-stats", payload="{}", endpoint="http://cp:8080", executor=executor, role="host"
    ).run(TaskInputs.empty())

    assert "-fsS" in executor.seen[0].argv


def test_the_scrape_check_names_the_sample_that_was_missing() -> None:
    """Two `grep -F` calls joined by `&&` exited 1 and said nothing: a missing
    metric and an unexpected failure counter looked identical."""
    task = PrometheusScrapeCheckTask(
        url="http://edge:18081/actuator/prometheus",
        executor=ScriptedExecutor(stdout="jvm_memory_used_bytes 1.0\n"),
        role="host",
        expect=('nanofaas_offload_total{function="word-stats",trigger="eager"}',),
    )

    with pytest.raises(RuntimeError, match=r"expected sample not scraped: nanofaas_offload_total"):
        task.run(TaskInputs.empty())


def test_the_scrape_check_names_the_sample_that_should_not_be_there() -> None:
    task = PrometheusScrapeCheckTask(
        url="http://edge:18081/actuator/prometheus",
        executor=ScriptedExecutor(stdout="nanofaas_offload_failure_total 3.0\n"),
        role="host",
        reject=("nanofaas_offload_failure_total",),
    )

    with pytest.raises(RuntimeError, match="present and should not be"):
        task.run(TaskInputs.empty())


def test_the_scrape_check_passes_when_both_halves_hold() -> None:
    task = PrometheusScrapeCheckTask(
        url="http://edge:18081/actuator/prometheus",
        executor=ScriptedExecutor(stdout='nanofaas_offload_total{trigger="eager"} 4.0\n'),
        role="host",
        expect=('nanofaas_offload_total{trigger="eager"}',),
        reject=("nanofaas_offload_failure_total",),
    )

    _ = task.run(TaskInputs.empty())


def test_a_scrape_check_that_asserts_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="expect or reject"):
        _ = PrometheusScrapeCheckTask(
            url="http://edge:18081/actuator/prometheus",
            executor=ScriptedExecutor(),
            role="host",
        )


def test_the_status_check_accepts_the_status_it_was_told_to_expect() -> None:
    executor = ScriptedExecutor(stdout="502")

    task = HttpStatusCheckTask(
        url="http://edge:18080/v1/functions/word-stats:invoke",
        expected_status=502,
        executor=executor,
        role="host",
        payload="{}",
    )
    _ = task.run(TaskInputs.empty())

    argv = executor.seen[0].argv
    # Not -f: a 502 has to be read, not turned into a non-zero exit.
    assert "-f" not in argv
    assert ("-w", "%{http_code}") == argv[argv.index("-w") : argv.index("-w") + 2]


def test_a_working_offload_fails_the_no_fallback_check() -> None:
    """200 here would mean the edge answered it locally, which is the thing this
    workflow exists to rule out."""
    task = HttpStatusCheckTask(
        url="http://edge:18080/v1/functions/word-stats:invoke",
        expected_status=502,
        executor=ScriptedExecutor(stdout="200"),
        role="host",
    )

    with pytest.raises(RuntimeError, match="answered 200, expected 502"):
        task.run(TaskInputs.empty())
