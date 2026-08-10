from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

Argv = tuple[str, ...] | Callable[[TaskInputs], tuple[str, ...]]


@dataclass
class CommandTask(Task[TaskResult]):
    """One command, run through a role-bound executor, as a Sonata task.

    Sonata deliberately owns no remote execution, so the executor comes from
    `sonata_tasks.execution`. What this class adds is the Sonata contract: a title the
    compiler slugifies into the task's identity, and a `TaskOutcome` carrying
    the command result as an in-process value.

    `verify` turns a command into a checked command: it runs only after the
    command itself succeeded, and raising from it fails the task. That is how a
    shell pipeline of `grep`s becomes a Python assertion.
    """

    title: str
    argv: Argv
    executor: CommandTaskExecutor
    role: ExecutionRole = "host"
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    cwd: Path | None = None
    remote_dir: str | None = None
    expected_exit_codes: frozenset[int] = field(default_factory=lambda: frozenset({0}))
    timeout_seconds: int | None = None
    verify: Callable[[TaskResult], None] | None = None

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[TaskResult]:
        result = self.executor.run(self._spec(inputs))
        if result.status != "passed":
            detail = "\n".join(part for part in (result.stderr.strip(), result.stdout.strip()) if part)
            detail = detail or "no output"
            raise RuntimeError(f"{self.title} failed (exit {result.return_code}): {detail}")
        if self.verify is not None:
            self.verify(result)
        return TaskOutcome(value=result)

    def _spec(self, inputs: TaskInputs) -> CommandTaskSpec:
        # task_id is empty on purpose: identity belongs to the compiler, and
        # CommandTaskSpec only uses this field to label its TaskResult.
        return CommandTaskSpec(
            task_id="",
            summary=self.title,
            argv=self.argv(inputs) if callable(self.argv) else self.argv,
            role=self.role,
            env=self.env,
            cwd=self.cwd,
            remote_dir=self.remote_dir,
            expected_exit_codes=self.expected_exit_codes,
            timeout_seconds=self.timeout_seconds,
        )
