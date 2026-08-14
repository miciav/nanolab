from __future__ import annotations

import time
from collections.abc import Callable

from sonata_engine.workflow.events import WorkflowEvent
from sonata_engine.workflow.models import WorkflowState
from nanolab.tui.models import (
    TuiPhaseSnapshot,
    TuiWorkflowSnapshot,
)


class WorkflowEventAggregator:
    def __init__(
        self,
        *,
        planned_steps: list[str] | None = None,
        log_limit: int = 200,
    ) -> None:
        if log_limit <= 0:
            raise ValueError("log_limit must be positive")
        self.log_limit = log_limit
        self._phases: list[TuiPhaseSnapshot] = []
        self._phase_by_task_id: dict[str, TuiPhaseSnapshot] = {}
        self._logs: list[str] = []
        self._show_logs = True
        for label in planned_steps or []:
            self._phases.append(TuiPhaseSnapshot(label=label))

    def snapshot(self) -> TuiWorkflowSnapshot:
        return TuiWorkflowSnapshot(
            phases=[self._clone_phase(phase) for phase in self._phases],
            logs=list(self._logs),
            show_logs=self._show_logs,
        )

    def toggle_logs(self) -> None:
        self._show_logs = not self._show_logs

    def append_log(self, message: str) -> None:
        self._logs.append(message)
        if len(self._logs) > self.log_limit:
            self._logs = self._logs[-self.log_limit :]

    def upsert_phase(
        self,
        label: str,
        *,
        task_id: str | None = None,
        detail: str = "",
        activate: bool = False,
    ) -> int:
        phase = self._upsert_top_level_phase(label, task_id=task_id, detail=detail)
        if activate:
            self._mark_phase_running(phase)
        return self._phases.index(phase) + 1

    def mark_phase_running(self, phase_index: int) -> None:
        phase = self._phases[phase_index - 1]
        self._mark_phase_running(phase)

    def mark_phase_success(self, phase_index: int, detail: str = "") -> None:
        phase = self._phases[phase_index - 1]
        self._mark_phase_success(phase, detail=detail)

    def mark_phase_failed(self, phase_index: int, detail: str = "") -> None:
        phase = self._phases[phase_index - 1]
        self._mark_phase_failed(phase, detail=detail)

    def mark_phase_cancelled(self, phase_index: int, detail: str = "") -> None:
        phase = self._phases[phase_index - 1]
        self._mark_phase_cancelled(phase, detail=detail)

    def complete_running_phases(
        self,
        *,
        status: WorkflowState = "success",
        detail: str = "",
    ) -> None:
        finished_at = time.time()

        def complete_phase(phase: TuiPhaseSnapshot) -> None:
            if phase.status == "running":
                phase.status = status
                if detail:
                    phase.detail = detail
                phase.finished_at = finished_at
            for child in phase.children:
                complete_phase(child)

        for phase in self._phases:
            complete_phase(phase)

    # The engine bus emits the single task vocabulary (started/passed/failed,
    # plus skipped and log.line); one aggregator reads it.
    def handle_event(self, event: WorkflowEvent) -> None:
        if event.kind == "log.line":
            self._handle_log_line(event)
            return
        if event.kind == "task.started":
            self._append_task_log(event, "[step]")
            self._mark_phase_for_event(event, self._mark_phase_running)
            return
        if event.kind == "task.passed":
            self._append_task_log(event, "[ok]")
            self._mark_phase_for_event(event, self._mark_phase_success, with_detail=True)
            return
        if event.kind == "task.failed":
            self._append_task_log(event, "[fail]")
            self._mark_phase_for_event(event, self._mark_phase_failed, with_detail=True)
            return
        if event.kind == "task.skipped":
            self._append_task_log(event, "[skip]", with_detail=False)
            self._phase_for_event(event)

    def _handle_log_line(self, event: WorkflowEvent) -> None:
        if event.task_id:
            self._phase_for_event(event)
        prefix = "stderr │ " if event.stream == "stderr" else ""
        self.append_log(f"{prefix}{event.line}")

    def _append_task_log(
        self,
        event: WorkflowEvent,
        marker: str,
        *,
        with_detail: bool = True,
    ) -> None:
        title = event.title or event.task_id or "Task"
        self.append_log(
            f"{marker} {title}"
            + (f" ({event.detail})" if with_detail and event.detail else "")
        )

    def _mark_phase_for_event(
        self,
        event: WorkflowEvent,
        mark: Callable[..., None],
        *,
        with_detail: bool = False,
    ) -> None:
        phase = self._phase_for_event(event)
        if phase is not None:
            if with_detail:
                mark(phase, detail=event.detail)
            else:
                mark(phase)

    def _clone_phase(self, phase: TuiPhaseSnapshot) -> TuiPhaseSnapshot:
        return TuiPhaseSnapshot(
            label=phase.label,
            task_id=phase.task_id,
            parent_task_id=phase.parent_task_id,
            status=phase.status,
            detail=phase.detail,
            started_at=phase.started_at,
            finished_at=phase.finished_at,
            children=[self._clone_phase(child) for child in phase.children],
        )

    def _phase_for_event(
        self,
        event: WorkflowEvent,
    ) -> TuiPhaseSnapshot | None:
        if event.parent_task_id:
            return self._child_phase_for_event(event)
        return self._top_level_phase_for_event(event)

    def _existing_phase_for_task(self, event: WorkflowEvent) -> TuiPhaseSnapshot | None:
        phase = self._phase_by_task_id.get(event.task_id or "")
        if phase is not None:
            self._sync_phase_metadata(phase, event.title or phase.label, event.task_id, event.detail)
        return phase

    def _child_phase_for_event(self, event: WorkflowEvent) -> TuiPhaseSnapshot | None:
        parent = self._phase_by_task_id.get(event.parent_task_id or "")
        if parent is None:
            return None
        phase = self._existing_phase_for_task(event)
        if phase is not None:
            return phase
        return self._ensure_child_phase(
            parent,
            event.title or event.task_id or "Task",
            task_id=event.task_id,
            detail=event.detail,
        )

    def _top_level_phase_for_event(self, event: WorkflowEvent) -> TuiPhaseSnapshot | None:
        phase = self._existing_phase_for_task(event)
        if phase is not None:
            return phase
        phase = self._next_unassigned_top_level_phase()
        if phase is None:
            # No pre-planned slot available — create a dynamic phase for any event with a task_id.
            if event.task_id:
                return self._upsert_top_level_phase(
                    event.title or event.task_id,
                    task_id=event.task_id,
                    detail=event.detail,
                )
            return None
        if event.title and event.title != phase.label:
            return None
        self._sync_phase_metadata(phase, event.title or phase.label, event.task_id, event.detail)
        return phase

    def _upsert_top_level_phase(
        self,
        label: str,
        *,
        task_id: str | None = None,
        detail: str = "",
    ) -> TuiPhaseSnapshot:
        phase = self._phase_by_task_id.get(task_id or "")
        if phase is not None and phase.parent_task_id is None:
            self._sync_phase_metadata(phase, label, task_id, detail)
            return phase
        if task_id is None:
            for candidate in self._phases:
                if candidate.parent_task_id is None and candidate.task_id is None and candidate.label == label:
                    self._sync_phase_metadata(candidate, label, task_id, detail)
                    return candidate
        phase = self._next_unassigned_top_level_phase()
        if phase is None:
            phase = TuiPhaseSnapshot(label=label, task_id=task_id, detail=detail)
            self._phases.append(phase)
        else:
            self._sync_phase_metadata(phase, label, task_id, detail)
        if task_id:
            self._phase_by_task_id[task_id] = phase
        return phase

    def _next_unassigned_top_level_phase(self) -> TuiPhaseSnapshot | None:
        for phase in self._phases:
            if phase.parent_task_id is None and phase.task_id is None:
                return phase
        return None

    def _ensure_child_phase(
        self,
        parent: TuiPhaseSnapshot,
        label: str,
        *,
        task_id: str | None = None,
        detail: str = "",
    ) -> TuiPhaseSnapshot:
        phase = self._phase_by_task_id.get(task_id or "")
        if phase is not None and phase.parent_task_id == parent.task_id:
            self._sync_phase_metadata(phase, label, task_id, detail)
            return phase
        if task_id is None:
            for child in parent.children:
                if child.label == label and child.parent_task_id == parent.task_id:
                    self._sync_phase_metadata(child, label, task_id, detail)
                    return child
        phase = TuiPhaseSnapshot(
            label=label,
            task_id=task_id,
            parent_task_id=parent.task_id,
            detail=detail,
        )
        parent.children.append(phase)
        if task_id:
            self._phase_by_task_id[task_id] = phase
        return phase

    def _sync_phase_metadata(
        self,
        phase: TuiPhaseSnapshot,
        label: str,
        task_id: str | None,
        detail: str,
    ) -> None:
        if label and (not phase.label or phase.label == phase.task_id):
            phase.label = label
        if task_id and not phase.task_id:
            phase.task_id = task_id
            self._phase_by_task_id[task_id] = phase
        if detail:
            phase.detail = detail

    def _mark_phase_running(self, phase: TuiPhaseSnapshot) -> None:
        if phase.status != "running":
            phase.status = "running"
        phase.started_at = phase.started_at or time.time()

    def _mark_phase_success(self, phase: TuiPhaseSnapshot, detail: str = "") -> None:
        phase.status = "success"
        if detail:
            phase.detail = detail
        phase.finished_at = time.time()

    def _mark_phase_failed(self, phase: TuiPhaseSnapshot, detail: str = "") -> None:
        phase.status = "failed"
        if detail:
            phase.detail = detail
        phase.finished_at = time.time()

    def _mark_phase_cancelled(self, phase: TuiPhaseSnapshot, detail: str = "") -> None:
        phase.status = "cancelled"
        if detail:
            phase.detail = detail
        phase.finished_at = time.time()
