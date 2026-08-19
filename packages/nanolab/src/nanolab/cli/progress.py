from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
import time

import typer
from sonata_engine.workflow.events import WorkflowEvent

# One vocabulary, one console renderer: the engine bus emits task.started,
# task.passed and task.failed, and the sink renders status from those.
_RUNNING_KINDS = frozenset({"task.started"})
_TERMINAL_KINDS = frozenset({"task.passed", "task.failed"})
# Every line a command writes reaches the bus already: SubprocessShell forwards
# stdout and stderr through `workflow_log`, which builds a `log.line` event. The
# console renderer simply never looked at them, so a task that runs for twenty
# minutes showed two lines and nothing between — and telling "compiling" from
# "wedged" meant reading the VM's load average by hand.
_LOG_KIND = "log.line"


class ConsoleProgressSink:
    def __init__(
        self,
        *,
        write: Callable[[str], object] = typer.echo,
        clock: Callable[[], float] = time.monotonic,
        log_lines: bool = False,
    ) -> None:
        self._write = write
        self._clock = clock
        # Off by default, and deliberately: a k6 run or a helm install would bury
        # the task list under its own output. It earns its place where the task is
        # long and its output is progress — an image build is the case it exists
        # for.
        self._log_lines = log_lines
        self._started: dict[str, float] = {}
        self.records: list[dict[str, object]] = []

    def emit(self, event: WorkflowEvent) -> None:
        if event.kind == _LOG_KIND:
            if self._log_lines and event.line:
                self._write(event.line.rstrip())
            return
        task_id = event.task_id
        if task_id is None:
            return
        if event.kind in _RUNNING_KINDS:
            self._started[task_id] = self._clock()
            self._write(f"[{task_id}] running  {event.title}")
            return
        if event.kind not in _TERMINAL_KINDS:
            return
        now = self._clock()
        elapsed = now - self._started.pop(task_id, now)
        status = "failed" if event.kind == "task.failed" else "passed"
        self.records.append(
            {
                "task_id": task_id,
                "title": event.title,
                "status": status,
                "duration_seconds": round(elapsed, 3),
                "detail": event.detail,
            }
        )
        detail = f"  {event.detail}" if event.detail else ""
        self._write(f"[{task_id}] {status:<8} {elapsed:.1f}s{detail}")

    @contextmanager
    def status(self, label: str) -> Generator[None, None, None]:
        yield
