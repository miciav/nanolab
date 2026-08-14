from __future__ import annotations

from collections.abc import Callable
import traceback
from typing import Any

from rich.console import Console
from rich.live import Live

from nanolab.tui.event_aggregator import WorkflowEventAggregator
from nanolab.tui.models import TuiPhaseSnapshot, TuiWorkflowSnapshot
from nanolab.tui.workflow import TuiWorkflowSink, WorkflowDashboard, WorkflowKeyListener
from sonata_engine.workflow.context import bind_workflow_sink
from sonata_engine.workflow.event_builders import build_task_event
from tui_toolkit.console import console as default_console


def _flatten_phase_snapshots(snapshot: TuiWorkflowSnapshot) -> list[TuiPhaseSnapshot]:
    phases: list[TuiPhaseSnapshot] = []

    def collect(items: list[TuiPhaseSnapshot]) -> None:
        for item in items:
            phases.append(item)
            collect(item.children)

    collect(snapshot.phases)
    return phases


def _failure_targets(phases: list[TuiPhaseSnapshot]) -> list[TuiPhaseSnapshot]:
    targets = [phase for phase in phases if phase.status == "running"]
    if not targets:
        pending = next(
            (phase for phase in phases if phase.status == "pending"),
            None,
        )
        targets = [pending] if pending is not None else []
    if not targets:
        targets = [TuiPhaseSnapshot(label="Workflow failed", task_id="workflow.failure")]
    return targets


def _handle_action_failure(
    dashboard: WorkflowDashboard,
    sink: TuiWorkflowSink,
    aggregator: WorkflowEventAggregator,
    exc: BaseException,
) -> BaseException:
    dashboard.error_detail = traceback.format_exc(limit=12)
    phases = _flatten_phase_snapshots(aggregator.snapshot())
    for phase in _failure_targets(phases):
        sink.emit(
            build_task_event(
                kind="task.failed",
                task_id=phase.task_id,
                parent_task_id=phase.parent_task_id,
                title=phase.label,
                detail=str(exc),
            )
        )
    return exc


def _record_error(
    error: BaseException | None,
    exc: BaseException,
    note: str,
) -> BaseException:
    if error is None:
        return exc
    error.add_note(note)
    return error


def _stop_listener(
    listener: WorkflowKeyListener,
    error: BaseException | None,
) -> BaseException | None:
    try:
        listener.stop()
    except (OSError, RuntimeError) as cleanup_error:
        error = _record_error(
            error,
            cleanup_error,
            f"Workflow listener cleanup failed: {cleanup_error}",
        )
    return error


class TuiWorkflowController:
    def __init__(self, *, console: Console = default_console) -> None:
        self.console = console

    def run_live_workflow(
        self,
        *,
        title: str,
        summary_lines: list[str],
        planned_steps: list[str] | None,
        action: Callable[[WorkflowDashboard, TuiWorkflowSink], Any],
    ) -> Any:
        aggregator = WorkflowEventAggregator(planned_steps=planned_steps)
        dashboard = WorkflowDashboard(
            title=title,
            breadcrumb=f"Main / {title}",
            footer_hint="l toggle logs | Ctrl+C exit",
            summary_lines=summary_lines,
            aggregator=aggregator,
        )
        live: Live | None = None

        def refresh() -> None:
            dashboard.sync_from_snapshot(aggregator.snapshot())
            if live is not None:
                live.update(dashboard.render(), refresh=True)

        sink = TuiWorkflowSink(aggregator, refresh=refresh)
        listener = WorkflowKeyListener(dashboard, refresh)
        result: Any = None
        error: BaseException | None = None

        self.console.clear()
        with Live(
            dashboard.render(),
            console=self.console,
            refresh_per_second=8,
            transient=False,
        ) as active_live:
            live = active_live
            refresh()
            try:
                listener.start()
                # Command output routing (SubprocessShell._emit_output) reads the
                # same contextvar, so one bind covers the whole execution layer.
                with bind_workflow_sink(sink):
                    try:
                        result = action(dashboard, sink)
                    except Exception as exc:
                        error = _handle_action_failure(dashboard, sink, aggregator, exc)
                dashboard.footer_hint = "Press any key to continue"
                refresh()
                if listener.input_is_tty:
                    listener.wait_for_acknowledgment()
            except (OSError, RuntimeError) as exc:
                error = _record_error(
                    error,
                    exc,
                    f"Additional live workflow error: {exc}",
                )
            finally:
                error = _stop_listener(listener, error)

        if error is not None:
            raise error
        return result


__all__ = ["TuiWorkflowController"]
