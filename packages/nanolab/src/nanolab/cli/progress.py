from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
import time

import typer
from sonata_engine.workflow.events import WorkflowEvent as SonataWorkflowEvent
from workflow_tasks.workflow.events import WorkflowEvent

# Two engines, one console renderer: workflow_tasks says running/completed,
# Sonata says started/passed. The events are structurally identical, so the
# sink only has to accept both vocabularies.
_RUNNING_KINDS = frozenset({"task.running", "task.started"})
_TERMINAL_KINDS = frozenset({"task.completed", "task.passed", "task.failed"})


class ConsoleProgressSink:
    def __init__(
        self,
        *,
        write: Callable[[str], object] = typer.echo,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._write = write
        self._clock = clock
        self._started: dict[str, float] = {}
        self.records: list[dict[str, object]] = []

    def emit(self, event: WorkflowEvent | SonataWorkflowEvent) -> None:
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
