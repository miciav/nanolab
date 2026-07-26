from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from sonata_engine import Resource, Workflow
from workflow_tasks.execution.bindings import (
    CommandTaskExecutor,
    RoleBindings,
    RoleBoundCommandTaskExecutor,
)
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask


@dataclass(frozen=True, slots=True)
class CliFunction:
    """One function the CLI workflow registers, invokes, and removes."""

    name: str
    image: str
    payload: str
    resources: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class CliWorkflowRequest:
    """Everything the CLI workflow needs that is not an executor."""

    functions: tuple[CliFunction, ...]
    cli_role: ExecutionRole = "host"
    namespace: str = "nanofaas-e2e"
    endpoint: str = "http://127.0.0.1:8080"
    binary: str = "clients/cli/build/install/nanofaas-cli/bin/nanofaas-cli"

    def __post_init__(self) -> None:
        if self.cli_role not in ("host", "stack"):
            raise ValueError("CLI workflow can run only on host or stack")
        if not self.functions:
            raise ValueError("CLI workflow requires at least one function")


def _verify_invocation(result: TaskResult) -> None:
    """Assert the control plane reported a usable invocation.

    This is the whole point of porting invoke to a real task: the old workflow
    piped the response through two `grep -q` calls inside a bash string, which
    could not tell "not JSON" from "status was error".
    """
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(
            f"invocation response was not JSON: {result.stdout[:200]!r}"
        ) from error
    if not isinstance(response, dict):
        raise RuntimeError(f"invocation response was not JSON object: {result.stdout[:200]!r}")
    if response.get("status") != "success":
        raise RuntimeError(f"invocation did not report success: {response.get('status')!r}")
    if "output" not in response:
        raise RuntimeError("invocation carried no output")


def _cli_argv(request: CliWorkflowRequest, *arguments: str) -> tuple[str, ...]:
    return (
        request.binary,
        "--endpoint",
        request.endpoint,
        "--namespace",
        request.namespace,
        *arguments,
    )


def _apply_script(request: CliWorkflowRequest, function: CliFunction) -> str:
    """Shell that writes the manifest next to the CLI and applies it.

    The manifest is created with `mktemp` on the target on purpose. Writing it
    here with `tempfile` would work on the host role and silently break on the
    stack role, where the CLI runs elsewhere and would never see a local path.
    """
    body: dict[str, object] = {
        "name": function.name,
        "image": function.image,
        "executionMode": "DEPLOYMENT",
        "timeoutMs": 5000,
        "concurrency": 2,
        "queueSize": 20,
        "maxRetries": 3,
    }
    if function.resources is not None:
        body["resources"] = function.resources
    manifest = json.dumps(body, separators=(",", ":"))
    apply_command = " ".join(
        shlex.quote(value)
        for value in _cli_argv(request, "fn", "apply", "--file", "$manifest")
    ).replace("'$manifest'", '"$manifest"')
    return (
        f"manifest=$(mktemp); trap 'rm -f \"$manifest\"' EXIT; "
        f"printf '%s' {shlex.quote(manifest)} > \"$manifest\"; " + apply_command
    )


def _function_resource(
    request: CliWorkflowRequest,
    function: CliFunction,
    executor: CommandTaskExecutor,
    cwd: Path | None,
) -> Resource:
    """The registered function as an acquire/release pair.

    Sonata splices the apply before the first task that needs the function and
    the delete after the last one, and runs the delete even when a consumer
    fails. That is why the old separate `cleanup_specs` list is gone.
    """
    apply_task = CommandTask(
        title=f"Apply {function.name}",
        argv=("bash", "-lc", _apply_script(request, function)),
        executor=executor,
        role=request.cli_role,
        cwd=cwd,
    )
    delete_task = CommandTask(
        title=f"Delete {function.name}",
        argv=_cli_argv(request, "fn", "delete", function.name),
        executor=executor,
        role=request.cli_role,
        cwd=cwd,
    )

    def acquire() -> None:
        try:
            apply_task.run()
        except BaseException as error:
            # The apply may have registered the function before failing. The
            # engine will not release an acquire that did not pass, so the
            # compensation has to happen here, best-effort.
            try:
                delete_task.run()
            except BaseException as cleanup_error:
                error.add_note(f"Best-effort delete after a failed apply failed: {cleanup_error}")
            raise

    def release() -> None:
        delete_task.run()

    return Resource(
        title=f"Acquire {function.name}",
        acquire=acquire,
        release=release,
    )


def build_cli_workflow(
    request: CliWorkflowRequest,
    bindings: RoleBindings,
    *,
    workflow_id: str = "cli",
    cwd: Path | None = None,
) -> Workflow:
    """Build the CLI end-to-end workflow: build, register, list, invoke, remove."""
    executor = RoleBoundCommandTaskExecutor(bindings)
    workflow = Workflow(workflow_id=workflow_id)
    workflow.add(
        CommandTask(
            title="Build nanofaas-cli",
            argv=("./gradlew", ":nanofaas-cli:installDist", "--no-daemon"),
            executor=executor,
            role=request.cli_role,
            cwd=cwd,
        )
    )
    resources = tuple(
        _function_resource(request, function, executor, cwd) for function in request.functions
    )
    workflow.add(
        CommandTask(
            title="List functions",
            argv=_cli_argv(request, "fn", "list"),
            executor=executor,
            role=request.cli_role,
            cwd=cwd,
        ),
        requires=resources,
    )
    for function, resource in zip(request.functions, resources):
        workflow.add(
            CommandTask(
                title=f"Invoke {function.name}",
                argv=_cli_argv(request, "invoke", function.name, "--data", function.payload),
                executor=executor,
                role=request.cli_role,
                cwd=cwd,
                verify=_verify_invocation,
            ),
            requires=(resource,),
        )
    return workflow
