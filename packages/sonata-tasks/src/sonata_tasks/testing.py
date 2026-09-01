"""Canned answers from a control plane that honours the whole CLI surface.

Shipped rather than kept in one test file because two suites drive the same
workflow — `sonata_tasks`' own tests and nanolab's plan tests — and a second
copy of these fixtures would drift the day a command's output changes.

Keyed by the fragment of the joined argv that identifies each command, ordered
because the `--replace` apply and the refusal that precedes it differ only by
that flag and by the queue size in the manifest they carry.
"""

from __future__ import annotations

from sonata_tasks.tasks.models import TaskResult

INVOCATION_SUCCESS = '{"status":"success","output":{"words":2}}'


def passed(stdout: str = "", *, return_code: int = 0, stderr: str = "") -> TaskResult:
    return TaskResult(
        task_id="", status="passed", return_code=return_code, stdout=stdout, stderr=stderr
    )


CLI_RESPONSES: tuple[tuple[str, TaskResult], ...] = (
    ("fn apply --replace", passed('{"name":"word-stats-java","queueSize":21}')),
    ('"queueSize":21', passed(return_code=1, stderr="Error: ... rerun with --replace ...")),
    ("fn update", passed('{"concurrency":3,"timeoutMs":9000,"maxRetries":1}')),
    ("fn replicas set", passed('{"function":"word-stats-java","replicas":2}')),
    ("fn replicas get", passed('{"name":"word-stats-java","desiredReplicas":2,"readyReplicas":2}')),
    ("control-plane info", passed('{"capabilities":{"functionUpdate":true,"replicas":true}}')),
    ("control-plane contract", passed("openapi: 3.1.0\npaths:\n  /v1/functions: {}\n")),
    (
        '"rateMaxPerSecond":-1',
        passed(return_code=1, stderr="Error: Runtime configuration is invalid: bad"),
    ),
    ("config validate", passed('{"valid":true}')),
    (
        "config patch",
        passed(
            '{"revision":1,"effectiveConfig":{"revision":1,'
            '"namespaces":{"control-plane":{"rateMaxPerSecond":999999}}}}'
        ),
    ),
    ("config get", passed('{"revision":0,"namespaces":{"control-plane":{"rateMaxPerSecond":1000000}}}')),
)


def cli_response(argv: tuple[str, ...]) -> TaskResult:
    """What a compliant control plane answers this command, or a plain success."""
    joined = " ".join(argv)
    for fragment, result in CLI_RESPONSES:
        if fragment in joined:
            return result
    return passed(INVOCATION_SUCCESS)


# The workflow's own shape, as titles: what it runs once against the control
# plane, what it runs against a runtime-config namespace when one is available,
# and what it runs against each registered function.
CLI_CONTRACT_STEPS = ("Control-plane info", "Control-plane contract")
CLI_RUNTIME_CONFIG_STEPS = (
    "Runtime config snapshot",
    "Validate runtime config",
    "Reject invalid runtime config",
    "Patch runtime config",
)


def cli_function_steps(name: str) -> tuple[str, ...]:
    return (
        f"Invoke {name}",
        f"Update {name}",
        f"Scale {name}",
        f"Replicas of {name}",
        f"Refuse unreplaced change to {name}",
        f"Replace {name}",
    )


def cli_task_ids(*titles: str) -> list[str]:
    """Compiled task ids for an ordered list of titles."""
    return [
        f"{ordinal:03d}.{title.lower().replace(' ', '-')}"
        for ordinal, title in enumerate(titles, 1)
    ]
