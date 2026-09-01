from __future__ import annotations

import json
import shlex
from dataclasses import replace
from pathlib import Path
from typing import Any

from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole

from sonata_tasks.command import CommandTask
from sonata_tasks.invocation import verify_invocation
from sonata_tasks.manifest import FunctionManifest
from sonata_tasks.tasks.models import TaskResult

# Stands in for the temp file the script creates on the target. Chosen so
# `shlex.quote` leaves it alone: it is always its own word, never a substring of
# one, so substituting the shell variable back in cannot corrupt a neighbour.
FILE = "@FILE@"


def _script_with_file(content: str, *commands: tuple[str, ...]) -> str:
    """Shell that writes `content` to a temp file and runs `commands` against it.

    The file is created with `mktemp` on the target on purpose. Writing it here
    with `tempfile` would work when the CLI runs on this host and silently break
    when it runs inside a VM, which would never see a local path.

    Commands are chained with `&&`: a probe that reads back what the previous
    command wrote must not report success when the write itself failed.
    """
    rendered = " && ".join(
        " ".join(shlex.quote(value) for value in command).replace(FILE, '"$manifest"')
        for command in commands
    )
    return (
        f"manifest=$(mktemp); trap 'rm -f \"$manifest\"' EXIT; "
        f"printf '%s' {shlex.quote(content)} > \"$manifest\"; " + rendered
    )


def _apply_script(manifest: FunctionManifest, cli_argv: tuple[str, ...]) -> str:
    return _script_with_file(manifest.json(), (*cli_argv, "fn", "apply", "--file", FILE))


def _json_stdout(result: TaskResult) -> dict[str, Any]:
    """The JSON object a CLI command printed, or a failure that says what came instead."""
    try:
        payload = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(f"CLI output was not JSON: {result.stdout[:200]!r}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"CLI output was not a JSON object: {result.stdout[:200]!r}")
    return payload


def _expect(payload: dict[str, Any], field: str, expected: object) -> None:
    if payload.get(field) != expected:
        raise RuntimeError(f"{field} is {payload.get(field)!r}, expected {expected!r}")


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


def control_plane_contract_tasks(
    *,
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> tuple[CommandTask, ...]:
    """`control-plane info` and `contract`, checked against what this workflow uses.

    The capabilities are asserted, not merely printed: every optional command
    below (`fn update`, `fn replicas`) refuses to run when the control plane does
    not advertise it, so a build that lost the endpoint would otherwise surface
    as a puzzling "not supported by this control-plane build" three tasks later.
    """

    def verify_info(result: TaskResult) -> None:
        capabilities = _json_stdout(result).get("capabilities")
        if not isinstance(capabilities, dict):
            raise RuntimeError(f"control-plane info carried no capabilities: {result.stdout[:200]!r}")
        missing = [
            name for name in ("functionUpdate", "replicas") if capabilities.get(name) is not True
        ]
        if missing:
            raise RuntimeError(f"control plane does not advertise {', '.join(missing)}")

    def verify_contract(result: TaskResult) -> None:
        if "openapi:" not in result.stdout or "/v1/functions" not in result.stdout:
            raise RuntimeError(f"contract is not an OpenAPI document: {result.stdout[:200]!r}")

    return (
        CommandTask(
            title="Control-plane info",
            argv=(*cli_argv, "control-plane", "info"),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify_info,
        ),
        CommandTask(
            title="Control-plane contract",
            argv=(*cli_argv, "control-plane", "contract"),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify_contract,
        ),
    )


def function_update_task(
    name: str,
    *,
    patch: dict[str, Any],
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> CommandTask:
    """`fn update` followed by the `fn get` that proves it landed.

    One task, not two: `fn update` prints nothing on success, so the chained
    `fn get` owns the whole stdout and the patch can be verified where it is
    applied. A patch that the control plane accepted but ignored is exactly the
    failure this exists to catch.
    """

    def verify(result: TaskResult) -> None:
        details = _json_stdout(result)
        for field, expected in patch.items():
            _expect(details, field, expected)

    return CommandTask(
        title=f"Update {name}",
        argv=(
            "bash",
            "-lc",
            _script_with_file(
                json.dumps(patch, separators=(",", ":")),
                (*cli_argv, "fn", "update", name, "--file", FILE),
                (*cli_argv, "fn", "get", name),
            ),
        ),
        executor=executor,
        role=role,
        cwd=cwd,
        verify=verify,
    )


def function_replicas_tasks(
    name: str,
    *,
    replicas: int,
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> tuple[CommandTask, ...]:
    """`fn replicas set` and the `get` that reads the desired count back.

    Two tasks rather than one chained script: `set` prints its own JSON, so a
    chain would hand the verifier two documents on one stdout.
    """

    def verify_set(result: TaskResult) -> None:
        _expect(_json_stdout(result), "replicas", replicas)

    def verify_get(result: TaskResult) -> None:
        _expect(_json_stdout(result), "desiredReplicas", replicas)

    return (
        CommandTask(
            title=f"Scale {name}",
            argv=(*cli_argv, "fn", "replicas", "set", name, str(replicas)),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify_set,
        ),
        CommandTask(
            title=f"Replicas of {name}",
            argv=(*cli_argv, "fn", "replicas", "get", name),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify_get,
        ),
    )


def function_replace_tasks(
    manifest: FunctionManifest,
    *,
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> tuple[CommandTask, ...]:
    """Both halves of the `--replace` contract, on one changed immutable field.

    `queueSize` is the field on purpose: the CLI treats it as immutable, and
    unlike the image or the resources it changes nothing the running function
    does, so the check costs a re-register and not a broken deployment.

    The refusal is the first half and is the one worth having: without it a
    changed immutable field would silently delete and re-create a function,
    which is what `--replace` exists to make someone type out loud.
    """
    changed = replace(manifest, queue_size=manifest.queue_size + 1)
    script = _script_with_file(
        changed.json(), (*cli_argv, "fn", "apply", "--file", FILE)
    )
    replace_script = _script_with_file(
        changed.json(),
        (*cli_argv, "fn", "apply", "--replace", "--file", FILE),
        (*cli_argv, "fn", "get", manifest.name),
    )

    def verify_refusal(result: TaskResult) -> None:
        if "--replace" not in result.stderr:
            raise RuntimeError(
                "apply of a changed immutable field failed without asking for --replace: "
                f"{(result.stderr or result.stdout)[:200]!r}"
            )

    def verify_replaced(result: TaskResult) -> None:
        _expect(_json_stdout(result), "queueSize", changed.queue_size)

    return (
        CommandTask(
            title=f"Refuse unreplaced change to {manifest.name}",
            argv=("bash", "-lc", script),
            executor=executor,
            role=role,
            cwd=cwd,
            expected_exit_codes=frozenset({1}),
            verify=verify_refusal,
        ),
        CommandTask(
            title=f"Replace {manifest.name}",
            argv=("bash", "-lc", replace_script),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify_replaced,
        ),
    )


def runtime_config_tasks(
    namespace: str,
    *,
    patch: dict[str, Any],
    invalid_patch: dict[str, Any],
    cli_argv: tuple[str, ...],
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    cwd: Path | None = None,
) -> tuple[CommandTask, ...]:
    """`control-plane config get|validate|patch` against a live namespace.

    The invalid patch is checked too: `validate` that never rejects anything is
    indistinguishable from `validate` that is not wired to the namespace at all.
    """
    prefix = (*cli_argv, "control-plane", "config")

    def verify_snapshot(result: TaskResult) -> None:
        snapshot = _json_stdout(result)
        if not isinstance(snapshot.get("revision"), int):
            raise RuntimeError(f"runtime config carried no revision: {result.stdout[:200]!r}")
        if namespace not in (snapshot.get("namespaces") or {}):
            raise RuntimeError(f"runtime config has no '{namespace}' namespace")

    def verify_valid(result: TaskResult) -> None:
        _expect(_json_stdout(result), "valid", True)

    def verify_rejected(result: TaskResult) -> None:
        if "is invalid" not in result.stderr:
            raise RuntimeError(
                f"invalid runtime config was not reported as invalid: {(result.stderr or result.stdout)[:200]!r}"
            )

    def verify_patched(result: TaskResult) -> None:
        applied = (
            _json_stdout(result).get("effectiveConfig", {}).get("namespaces", {}).get(namespace, {})
        )
        for field, expected in patch.items():
            _expect(applied, field, expected)

    def config_task(
        title: str,
        *arguments: str,
        content: dict[str, Any] | None = None,
        verify: Any,
        exit_codes: frozenset[int] = frozenset({0}),
    ) -> CommandTask:
        argv = (
            ("bash", "-lc", _script_with_file(
                json.dumps(content, separators=(",", ":")),
                (*prefix, *arguments, "--file", FILE),
            ))
            if content is not None
            else (*prefix, *arguments)
        )
        return CommandTask(
            title=title,
            argv=argv,
            executor=executor,
            role=role,
            cwd=cwd,
            expected_exit_codes=exit_codes,
            verify=verify,
        )

    return (
        config_task("Runtime config snapshot", "get", verify=verify_snapshot),
        config_task(
            "Validate runtime config",
            "validate",
            namespace,
            content=patch,
            verify=verify_valid,
        ),
        config_task(
            "Reject invalid runtime config",
            "validate",
            namespace,
            content=invalid_patch,
            verify=verify_rejected,
            exit_codes=frozenset({1}),
        ),
        config_task(
            "Patch runtime config",
            "patch",
            namespace,
            content=patch,
            verify=verify_patched,
        ),
    )
